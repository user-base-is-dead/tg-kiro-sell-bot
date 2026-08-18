from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_cipher, new_order_number
from app.database.models.catalog import FulfillmentMode, Product, StockStatus
from app.database.models.order import Delivery, Order, OrderItem, OrderStatus, Warranty, WarrantyStatus
from app.database.models.wallet import TxnType
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.user_repo import UserRepo
from app.services import referral_service, stock_hold_service, wallet_service
from app.utils.errors import UserError

IDEMPOTENCY_WINDOW_SECONDS = 15


def checkout_idempotency_key(user_id: int, product_id: int, qty: int = 1) -> str:
    # The quantity is part of the key: it is a different purchase. Without it, a buyer who bought
    # one and immediately came back for three inside the same 15-second window was handed the
    # first order back and charged for one — the second purchase silently never happened.
    bucket = int(time.time() // IDEMPOTENCY_WINDOW_SECONDS)
    return f"buy:{user_id}:{product_id}:{qty}:{bucket}"


@dataclass(frozen=True)
class PlacedOrder:
    order: Order
    order_item: OrderItem
    # Plaintext, only for AUTO — shown once, never re-fetchable. One entry per unit bought.
    delivered_payloads: tuple[str, ...] = ()

    @property
    def delivered_payload(self) -> str | None:
        """The single-unit view, for the screens and callers that only ever deal in one."""
        return self.delivered_payloads[0] if self.delivered_payloads else None


async def _claim_stock(
    session: AsyncSession,
    order_repo: OrderRepo,
    product: Product,
    order_item: OrderItem,
    *,
    user_id: int | None = None,
):
    """Reserve ONE credential for `order_item`, or raise if none can be had.

    The buyer's own held credential comes first: they were promised *that* login when they picked a
    payment method, and `claim_held` flips it HELD → RESERVED atomically, so if the expiry sweep got
    there first this falls through rather than delivering a credential somebody else may now hold.

    Only then does it take a fresh AVAILABLE one — the path for gift orders and for a buyer whose
    hold lapsed while a credential happens to be free again. Either way the shelf is consulted
    before the wallet is, so an empty product never costs anyone money.
    """
    if user_id is not None:
        held = await stock_hold_service.get_hold(session, product.id, user_id)
        if held is not None and await stock_hold_service.claim_held(session, held.id, user_id):
            held.order_item_id = order_item.id
            await session.flush()
            return held

    claimed = await order_repo.claim_available_stock(product.id, 1)
    if not claimed:
        raise UserError("errors.out_of_stock")
    stock_item = claimed[0]
    stock_item.status = StockStatus.RESERVED
    stock_item.order_item_id = order_item.id
    await session.flush()
    return stock_item


async def _claim_stock_many(
    session: AsyncSession,
    order_repo: OrderRepo,
    product: Product,
    order_item: OrderItem,
    qty: int,
    *,
    user_id: int,
) -> list:
    """Reserve `qty` credentials, or as many as can be had. Never partially charges anyone.

    The buyer's own holds are taken first and in full — they were promised *those* logins on the
    payment screen — and only then does it reach for whatever else is free. A short list is
    returned rather than raised on, because whether that ends the sale depends on the product: with
    a hand-set count the admin has promised units the pool does not contain, and those are fulfilled
    by hand instead of refused.
    """
    claimed: list = []

    for held in await stock_hold_service.get_holds(session, product.id, user_id):
        if len(claimed) >= qty:
            break
        if await stock_hold_service.claim_held(session, held.id, user_id):
            held.order_item_id = order_item.id
            claimed.append(held)

    if len(claimed) < qty:
        for stock_item in await order_repo.claim_available_stock(product.id, qty - len(claimed)):
            stock_item.status = StockStatus.RESERVED
            stock_item.order_item_id = order_item.id
            claimed.append(stock_item)

    await session.flush()
    return claimed


def _unclaim(session: AsyncSession, stock_items: list) -> None:
    """Put reserved credentials back on the shelf.

    Used when the pool could not cover the whole order and the sale falls back to hand fulfilment:
    holding half an order's worth of logins that will never be delivered would take them from
    buyers who can actually be served from the pool.
    """
    for stock_item in stock_items:
        stock_item.status = StockStatus.AVAILABLE
        stock_item.order_item_id = None


def _deliver_auto(
    session: AsyncSession, order: Order, order_item: OrderItem, stock_items: list, now: datetime
) -> list[str]:
    """Hand over the reserved stock items and close the order. Returns the plaintext payloads, which
    are shown to the user once and never persisted a second time — the ciphertext on the stock items
    stays the only stored copy."""
    cipher = get_cipher()
    payloads = []
    for stock_item in stock_items:
        stock_item.status = StockStatus.DELIVERED
        payloads.append(cipher.decrypt(stock_item.payload))
    session.add(Delivery(order_item_id=order_item.id, mode="AUTO", payload=None, delivered_at=now))
    order.status = OrderStatus.COMPLETED
    order.completed_at = now
    return payloads


def _start_warranty(session: AsyncSession, product: Product, order_item: OrderItem, user_id: int, now: datetime) -> None:
    """Only ever called from `place_order` — a warranty is something a purchase buys.

    Giveaways deliberately have no path here. Gift items are their own stock (`GiftItem`, migration
    0015) and are handed over without an order at all, so there is nothing to attach a warranty to
    and no way for a free item to end up entitled to a replacement. A `place_gift_order` used to
    exist that created an order, a delivery *and* a warranty for a catalog product handed out by a
    code; it was already unreachable once the PRODUCT gift kind was dropped, and it is gone.
    """
    if product.warranty_days > 0:
        session.add(
            Warranty(
                order_item_id=order_item.id,
                user_id=user_id,
                starts_at=now,
                expires_at=now + timedelta(days=product.warranty_days),
                status=WarrantyStatus.ACTIVE,
            )
        )


async def place_order(
    session: AsyncSession, *, user_id: int, product_id: int, qty: int = 1
) -> PlacedOrder:
    """The whole purchase in one DB transaction: claim stock -> debit wallet -> deliver.
    Any failure (out of stock, insufficient balance) rolls the entire thing back — nothing
    is ever half-applied.

    `qty` is checked against the shelf here and not only in the screen that asked for it. The
    number was typed minutes earlier and other buyers have been shopping since; the transaction is
    the only place that can promise it still holds.
    """
    qty = int(qty)
    if qty < 1:
        raise UserError("errors.invalid_quantity")

    product_repo = ProductRepo(session)
    product: Product | None = await product_repo.get_by_id(product_id)
    if product is None or not product.is_active or product.status.value in ("COMING_SOON", "DISABLED"):
        raise UserError("errors.product_unavailable")

    # A hand-set count closes the product to buyers even while credentials sit on the shelf, and
    # caps how many of them can go in one order. It has to be checked here rather than left to the
    # claim, which would happily hand those credentials over.
    if product.manual_stock is not None and product.manual_stock < qty:
        raise UserError("errors.out_of_stock")

    idempotency_key = checkout_idempotency_key(user_id, product_id, qty)
    order_repo = OrderRepo(session)
    existing = await order_repo.get_by_idempotency_key(idempotency_key)
    if existing is not None:
        existing_full = await order_repo.get_by_id(existing.id)
        return PlacedOrder(order=existing_full, order_item=existing_full.items[0])

    total_minor = product.price_minor * qty
    now = datetime.now(UTC)
    order = Order(
        order_number=new_order_number(),
        user_id=user_id,
        status=OrderStatus.PENDING,
        subtotal_minor=total_minor,
        discount_minor=0,
        total_minor=total_minor,
        currency=product.currency,
        idempotency_key=idempotency_key,
        placed_at=now,
    )
    session.add(order)
    await session.flush()

    order_item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        product_name=product.name,
        unit_price_minor=product.price_minor,
        qty=qty,
        warranty_days=product.warranty_days,
    )
    session.add(order_item)
    await session.flush()

    delivered_payloads: list[str] = []

    # Stock first, then payment, then delivery: an empty shelf must not cost the buyer money, and a
    # failed debit must not hand over the goods.
    stock_items: list = []
    if product.fulfillment_mode == FulfillmentMode.AUTO:
        stock_items = await _claim_stock_many(session, order_repo, product, order_item, qty, user_id=user_id)
        if len(stock_items) < qty:
            # The pool cannot cover the whole order. That is only the end of the sale when the pool
            # is what defines availability — under a hand-set count the admin has promised units
            # beyond it, so the order is sold and fulfilled by hand instead. It goes by hand in
            # *full*: splitting it would deliver half now and leave the buyer wondering whether the
            # rest is coming, and the credentials are worth more back on the shelf.
            if product.manual_stock is None:
                raise UserError("errors.out_of_stock")
            _unclaim(session, stock_items)
            stock_items = []
            await session.flush()

    debit_txn = await wallet_service.debit(
        session,
        user_id=user_id,
        amount_minor=total_minor,
        currency=product.currency,
        type_=TxnType.PURCHASE,
        idempotency_key=idempotency_key,
        ref_type="order",
        ref_id=order.id,
    )
    order.payment_txn_id = debit_txn.id

    if stock_items:
        delivered_payloads = _deliver_auto(session, order, order_item, stock_items, now)
    else:
        order.status = OrderStatus.PROCESSING

    # The override is a counter, not a label: whatever the sale cost the credential pool, it also
    # costs the number the admin typed, or the storefront would keep advertising a count that no
    # longer exists.
    if product.manual_stock is not None:
        product.manual_stock = max(0, product.manual_stock - qty)

    _start_warranty(session, product, order_item, user_id, now)

    await session.flush()

    if order.status == OrderStatus.COMPLETED:
        buyer = await UserRepo(session).get_by_id(user_id)
        if buyer is not None:
            await referral_service.try_qualify_referral(session, user_id=user_id, referred_by_id=buyer.referred_by_id)

    return PlacedOrder(order=order, order_item=order_item, delivered_payloads=tuple(delivered_payloads))


async def notify_admins_of_manual_order(bot, session: AsyncSession, order: Order) -> bool:
    """DM every admin that a hand-fulfilled order is waiting, with a button straight to it.

    Manual fulfilment had no push at all: an order went PROCESSING and sat in the Pending
    Fulfilment list until an admin happened to open the admin panel and look. From the buyer's side
    that is indistinguishable from the feature being broken — they get "being prepared" and then
    nothing, for as long as nobody checks.

    Best-effort by design. The order is already committed to the database and the queue screen is
    still the source of truth, so a bot blocked by one admin must never fail the purchase. Returns
    whether at least one admin was actually reached.
    """
    import logging

    from aiogram.exceptions import TelegramAPIError
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.bot.callbacks import AdminOrderCB
    from app.core.config import get_settings

    buyer = await UserRepo(session).get_by_id(order.user_id)
    who = f"@{buyer.username}" if buyer and buyer.username else f"id {order.user_id}"
    # Re-fetched through the repo for its eager load: `place_order` hands back an Order whose
    # `items` collection has never been loaded, and touching it here would lazy-load mid-async.
    full = await OrderRepo(session).get_by_id(order.id) or order
    items = "\n".join(f"• {item.product_name} x{item.qty}" for item in full.items)
    text = (
        "🙋 <b>Manual order awaiting fulfilment</b>\n\n"
        f"🛒 <code>{order.order_number}</code>\n"
        f"👤 Buyer: {who}\n"
        f"{items}\n\n"
        "The buyer has already paid. Open it to send their content."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Fulfill now",
                    callback_data=AdminOrderCB(action="view", id=order.id).pack(),
                )
            ]
        ]
    )

    delivered = False
    for admin_id in get_settings().admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
            delivered = True
        except TelegramAPIError as exc:
            logging.getLogger(__name__).warning(
                "Manual-order ping to admin %s failed (%s)", admin_id, exc
            )
    return delivered


async def fulfill_manual_order(
    session: AsyncSession, *, order_id: str, delivery_payload: str, admin_telegram_id: int
) -> Order:
    order = await OrderRepo(session).get_by_id(order_id)
    if order is None or order.status != OrderStatus.PROCESSING:
        raise UserError("common.unknown_action")

    now = datetime.now(UTC)
    for item in order.items:
        session.add(
            Delivery(
                order_item_id=item.id,
                mode="MANUAL",
                payload=delivery_payload,
                delivered_at=now,
                delivered_by_admin_id=admin_telegram_id,
            )
        )
    order.status = OrderStatus.COMPLETED
    order.completed_at = now
    await session.flush()

    buyer = await UserRepo(session).get_by_id(order.user_id)
    if buyer is not None:
        await referral_service.try_qualify_referral(session, user_id=order.user_id, referred_by_id=buyer.referred_by_id)

    return order


async def cancel_order(session: AsyncSession, *, order_id: str, reason: str) -> Order:
    """Refunds the wallet debit and releases any RESERVED (not yet DELIVERED) stock."""
    from sqlalchemy import select

    from app.database.models.catalog import StockItem

    order = await OrderRepo(session).get_by_id(order_id)
    if order is None or order.status in (OrderStatus.CANCELLED, OrderStatus.FAILED):
        raise UserError("common.unknown_action")

    now = datetime.now(UTC)

    if order.payment_txn_id is not None:
        await wallet_service.credit(
            session,
            user_id=order.user_id,
            amount_minor=order.total_minor,
            currency=order.currency,
            type_=TxnType.REFUND,
            idempotency_key=f"refund:{order.id}",
            ref_type="order",
            ref_id=order.id,
        )

    for item in order.items:
        result = await session.execute(
            select(StockItem).where(StockItem.order_item_id == item.id, StockItem.status == StockStatus.RESERVED)
        )
        for stock_item in result.scalars().all():
            stock_item.status = StockStatus.AVAILABLE
            stock_item.order_item_id = None

    order.status = OrderStatus.CANCELLED
    order.cancelled_at = now
    order.failure_reason = reason
    await session.flush()
    return order
