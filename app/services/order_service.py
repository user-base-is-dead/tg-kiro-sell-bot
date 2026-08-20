from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_cipher, new_order_number
from app.database.models.catalog import FulfillmentMode, Product, StockStatus
from app.database.models.order import (
    Delivery,
    FundingSource,
    Order,
    OrderItem,
    OrderStatus,
    RefundState,
    Warranty,
    WarrantyStatus,
)
from app.database.models.order_event import OrderEvent, OrderEventActor, OrderEventKind
from app.database.models.wallet import TxnType
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.user_repo import UserRepo
from app.services import order_event_service, referral_service, stock_hold_service, wallet_service
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


async def _claim_crypto_invoice(session: AsyncSession, order: Order, product_id: int) -> None:
    """Attach the on-chain payment this order was bought with, if there was one.

    Crypto never pays for an order directly — pressing 💎 Pay with Crypto opens a wallet top-up
    invoice tagged `buy:<product_id>:…`, the chain credits the wallet, and the wallet buys. So by the
    time we get here the order looks identical to one paid from a balance the buyer already had.

    It isn't identical to the person owed a refund. A balance can be handed back; a USDT transfer
    cannot be reversed, and refunding it means asking for an address and sending money by hand. This
    is where that difference gets written down: the most recent CONFIRMED invoice for this product
    that no other order has claimed becomes this order's funding source.

    "Not yet claimed" is what keeps it honest — an invoice is spent exactly once, so a buyer who pays
    on chain, buys, and then buys the same product again out of leftover balance gets CRYPTO on the
    first order and WALLET on the second. A plain Top Up Wallet invoice is tagged `topup:` and is
    never matched here: that money genuinely became their balance.
    """
    from sqlalchemy import select

    from app.database.models.crypto import CryptoPayment

    result = await session.execute(
        select(CryptoPayment)
        .where(
            CryptoPayment.user_id == order.user_id,
            CryptoPayment.status.in_(("CONFIRMED", "COMPLETED")),
            CryptoPayment.description.like(f"buy:{product_id}:%"),
            CryptoPayment.order_id.is_(None),
        )
        .order_by(CryptoPayment.confirmed_at.desc().nullslast(), CryptoPayment.id.desc())
        .limit(1)
    )
    payment = result.scalars().first()
    if payment is None:
        return

    payment.order_id = order.id
    order.funding_source = FundingSource.CRYPTO
    order.crypto_payment_id = payment.id


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

    # After the debit, before delivery: the invoice is only meaningful once the money has actually
    # moved, and the buyer's own screens need the funding source the moment the order exists.
    await _claim_crypto_invoice(session, order, product.id)

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

    # The order's history starts here, and it starts with an ID an admin can search on. Every later
    # event on this order — delivered, declined, refunded — hangs off the same order.
    await order_event_service.record(
        session,
        order,
        OrderEventKind.PLACED,
        actor=OrderEventActor.USER,
        amount_minor=order.total_minor,
        reason=f"{order_item.product_name} ×{qty}",
        reference=f"txn#{debit_txn.id}",
        at=now,
    )
    if order.status == OrderStatus.COMPLETED:
        await order_event_service.record(
            session,
            order,
            OrderEventKind.DELIVERED,
            reason=f"Auto-delivered {len(delivered_payloads)} item(s) from stock",
            at=now,
        )

    if order.status == OrderStatus.COMPLETED:
        buyer = await UserRepo(session).get_by_id(user_id)
        if buyer is not None:
            await referral_service.try_qualify_referral(session, user_id=user_id, referred_by_id=buyer.referred_by_id)

    return PlacedOrder(order=order, order_item=order_item, delivered_payloads=tuple(delivered_payloads))


async def notify_admins_of_manual_order(bot, session: AsyncSession, order: Order) -> bool:
    """Put a hand-fulfilled order in front of staff, with a button straight to it.

    It goes into the order's own topic in the orders group, under the card that already says what
    was bought and what was paid — so the work is done where the order lives, and every admin sees
    the same one copy of it get handled. DMing each admin separately, as this used to, scattered the
    same job across N private chats where nobody could see whether anybody had picked it up.

    Falls back to those DMs when there is no thread to post into (ORDERS_GROUP_ID unset, or the
    group refused the topic), because a manual order nobody is told about just sits in the queue:
    the buyer gets "being prepared" and then nothing, for as long as nobody checks.

    Best-effort by design. The order is already committed to the database and the queue screen is
    still the source of truth, so a blocked bot must never fail the purchase. Returns whether the
    prompt actually reached anybody.
    """
    import logging

    from aiogram.exceptions import TelegramAPIError
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.bot.callbacks import AdminOrderCB
    from app.core.config import get_settings
    from app.services import order_thread_service

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
    # Both answers, on the message that asks the question. An order waiting to be fulfilled has
    # exactly two endings, and offering only the happy one meant declining a bad order was a trip
    # back to the admin panel to find an order that was already on screen.
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Fulfill now",
                    # Straight to the "send the delivery content" prompt rather than via the
                    # dossier: in the thread the card above already IS the dossier.
                    callback_data=AdminOrderCB(action="fulfill", id=order.id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚫 Decline & Refund",
                    callback_data=AdminOrderCB(action="decline", id=order.id).pack(),
                )
            ],
        ]
    )

    # Opens the topic if this order has none yet, so there is somewhere to post.
    await order_thread_service.sync(bot, session, order)
    group_id = get_settings().orders_group_id
    if group_id is None:
        problem = "ORDERS_GROUP_ID isn't set."
    elif order.thread_id is None:
        problem = "the orders group wouldn't open a topic for this order."
    else:
        try:
            await bot.send_message(
                group_id, text, message_thread_id=order.thread_id, reply_markup=markup
            )
            return True
        except TelegramAPIError as exc:
            problem = f"the order thread refused it — {exc}"
            logging.getLogger(__name__).warning(
                "Manual-order prompt for %s couldn't be posted to the order thread (%s) — falling "
                "back to admin DMs",
                order.order_number,
                exc,
            )

    # The DM says why it is a DM. This is the fallback path, and an admin who was told this belongs
    # in the group has no way to tell a misconfigured group from a bot that never got restarted —
    # so the reason travels with the message instead of living only in a log file.
    text = f"{text}\n\n⚠️ <i>This should have gone to the order's thread, but {problem}</i>"

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

    await order_event_service.record(
        session,
        order,
        OrderEventKind.DELIVERED,
        actor=OrderEventActor.ADMIN,
        actor_telegram_id=admin_telegram_id,
        reason="Delivered by hand",
        at=now,
    )

    buyer = await UserRepo(session).get_by_id(order.user_id)
    if buyer is not None:
        await referral_service.try_qualify_referral(session, user_id=order.user_id, referred_by_id=buyer.referred_by_id)

    return order


async def _release_reserved_stock(session: AsyncSession, order: Order) -> None:
    """Put back any credential this order reserved but never handed over.

    DELIVERED rows are deliberately untouched: the buyer has seen that login, so it is spent whether
    or not the order survived. Only RESERVED stock returns to the shelf.
    """
    from sqlalchemy import select

    from app.database.models.catalog import StockItem

    for item in order.items:
        result = await session.execute(
            select(StockItem).where(StockItem.order_item_id == item.id, StockItem.status == StockStatus.RESERVED)
        )
        for stock_item in result.scalars().all():
            stock_item.status = StockStatus.AVAILABLE
            stock_item.order_item_id = None


@dataclass(frozen=True)
class DeclinedOrder:
    """What a decline produced, so the caller can report it without re-reading the database.

    `decline_event` and `refund_event` are the two IDs the admin and the buyer both get told. They are
    the handles that make the decline searchable afterwards — quoting one into the admin search bar
    lands back on this order.
    """

    order: Order
    decline_event: OrderEvent
    refund_event: OrderEvent | None
    refunded_minor: int


async def decline_order(
    session: AsyncSession, *, order_id: str, reason: str, admin_telegram_id: int | None = None
) -> DeclinedOrder:
    """Kill an order with a reason on the record, and park what was paid in the Refund Wallet.

    Three things happen and all three are permanent: the reason is written to the order (not only to a
    log an admin has to go looking for), the money leaves the store's books into a balance the buyer
    can see, and both get an ID that can be searched for later.

    The money does NOT go back into the spendable balance. That was the old behaviour and it quietly
    decided something the store hadn't: that a buyer who paid in USDT wants shop credit. Parking it
    means an admin chooses — send it on chain, move it across, or split it — with `refund_state`
    saying out loud that nobody has chosen yet.

    Idempotent on the money: `refund:<order.id>` is the same key the old refund path used, so an order
    that was already refunded under the previous behaviour cannot be paid twice.
    """
    order = await OrderRepo(session).get_by_id(order_id)
    # Only an order that has not been fulfilled can be declined. Refusing COMPLETED here as well as
    # in the UI is the point: declining a delivered order flipped it to CANCELLED and wrote a decline
    # reason onto something the buyer had already received, so the record no longer said what
    # happened. Money owed on a delivered order is a refund, not a decline.
    if order is None or order.status not in (OrderStatus.PENDING, OrderStatus.PROCESSING):
        raise UserError("common.unknown_action")

    now = datetime.now(UTC)
    reason = (reason or "").strip() or "No reason given"

    await _release_reserved_stock(session, order)

    order.status = OrderStatus.CANCELLED
    order.cancelled_at = now
    # Truncated to the column, but the untruncated text also lives on the DECLINED event below, whose
    # `reason` is TEXT — so the full wording survives even for a very long explanation.
    order.failure_reason = reason[:512]
    order.cancelled_by_admin_id = admin_telegram_id
    await session.flush()

    decline_event = await order_event_service.record(
        session,
        order,
        OrderEventKind.DECLINED,
        actor=OrderEventActor.ADMIN if admin_telegram_id else OrderEventActor.SYSTEM,
        actor_telegram_id=admin_telegram_id,
        reason=reason,
        at=now,
    )

    refund_event: OrderEvent | None = None
    refunded_minor = 0
    if order.payment_txn_id is not None and order.total_minor > 0:
        txn = await wallet_service.credit_refund_balance(
            session,
            user_id=order.user_id,
            amount_minor=order.total_minor,
            currency=order.currency,
            idempotency_key=f"refund:{order.id}",
            ref_type="order",
            ref_id=order.id,
        )
        refunded_minor = order.total_minor
        order.refund_state = RefundState.PARKED
        order.refund_amount_minor = refunded_minor
        await session.flush()

        refund_event = await order_event_service.record(
            session,
            order,
            OrderEventKind.REFUND_PARKED,
            actor=OrderEventActor.ADMIN if admin_telegram_id else OrderEventActor.SYSTEM,
            actor_telegram_id=admin_telegram_id,
            amount_minor=refunded_minor,
            reason="Held in Refund Wallet — not spendable until settled",
            reference=f"txn#{txn.id}",
            at=now,
        )

    await session.flush()
    return DeclinedOrder(
        order=order, decline_event=decline_event, refund_event=refund_event, refunded_minor=refunded_minor
    )


@dataclass(frozen=True)
class ParkedRefund:
    """What a refund on a delivered order produced. `event` is None only when there was no money to
    move, which is the one case the caller has to word differently."""

    order: Order
    event: OrderEvent | None
    amount_minor: int


async def refund_delivered_order(
    session: AsyncSession,
    *,
    order_id: str,
    amount_minor: int,
    reason: str,
    admin_telegram_id: int | None = None,
) -> ParkedRefund:
    """Park money back for an order that was delivered, without pretending it never happened.

    This is the counterpart to `decline_order`, and the difference is the whole point: a decline is a
    refusal to fulfil, so it cancels. Here the buyer got the product and is being paid back anyway —
    the honest record is a COMPLETED order carrying a refund, not a CANCELLED one with a decline
    reason on something that was delivered. Status, `completed_at` and the delivery event are all
    left exactly as they are.

    Shares `refund:<order.id>` with the decline path on purpose: whichever route pays first, the
    other cannot pay again.
    """
    order = await OrderRepo(session).get_by_id(order_id)
    if order is None or order.status is not OrderStatus.COMPLETED:
        raise UserError("common.unknown_action")
    # Refusing rather than topping up: `refund_amount_minor` is a single figure the settle screen
    # pays out against, and a second park would silently overwrite the first.
    if order.refund_state is not RefundState.NONE:
        raise UserError("common.unknown_action")

    amount_minor = max(0, min(int(amount_minor), order.total_minor))
    if order.payment_txn_id is None or amount_minor == 0:
        return ParkedRefund(order=order, event=None, amount_minor=0)

    now = datetime.now(UTC)
    reason = (reason or "").strip() or "Refunded by an admin"

    txn = await wallet_service.credit_refund_balance(
        session,
        user_id=order.user_id,
        amount_minor=amount_minor,
        currency=order.currency,
        idempotency_key=f"refund:{order.id}",
        ref_type="order",
        ref_id=order.id,
    )
    order.refund_state = RefundState.PARKED
    order.refund_amount_minor = amount_minor
    await session.flush()

    event = await order_event_service.record(
        session,
        order,
        OrderEventKind.REFUND_PARKED,
        actor=OrderEventActor.ADMIN if admin_telegram_id else OrderEventActor.SYSTEM,
        actor_telegram_id=admin_telegram_id,
        amount_minor=amount_minor,
        reason=reason,
        reference=f"txn#{txn.id}",
        at=now,
    )
    await session.flush()
    return ParkedRefund(order=order, event=event, amount_minor=amount_minor)


async def cancel_order(session: AsyncSession, *, order_id: str, reason: str) -> Order:
    """Back-compatible entry point: a decline with no admin attached.

    Kept because it is the name the rest of the codebase and any future job would reach for, and
    because a cancellation that skipped the event trail would be invisible in exactly the screen this
    work exists to fill.
    """
    return (await decline_order(session, order_id=order_id, reason=reason)).order
