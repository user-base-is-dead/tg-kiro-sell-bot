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
from app.services import order_hold_service, referral_service, wallet_service
from app.utils.errors import UserError

IDEMPOTENCY_WINDOW_SECONDS = 15


def checkout_idempotency_key(user_id: int, product_id: int) -> str:
    bucket = int(time.time() // IDEMPOTENCY_WINDOW_SECONDS)
    return f"buy:{user_id}:{product_id}:{bucket}"


@dataclass(frozen=True)
class PlacedOrder:
    order: Order
    order_item: OrderItem
    delivered_payload: str | None  # plaintext, only for AUTO — shown once, never re-fetchable


async def place_order(session: AsyncSession, *, user_id: int, product_id: int) -> PlacedOrder:
    """The whole purchase in one DB transaction: claim stock -> debit wallet -> deliver.
    Any failure (out of stock, insufficient balance) rolls the entire thing back — nothing
    is ever half-applied."""
    product_repo = ProductRepo(session)
    product: Product | None = await product_repo.get_by_id(product_id)
    if product is None or not product.is_active or product.status.value in ("COMING_SOON", "DISABLED"):
        raise UserError("errors.product_unavailable")

    idempotency_key = checkout_idempotency_key(user_id, product_id)
    order_repo = OrderRepo(session)
    existing = await order_repo.get_by_idempotency_key(idempotency_key)
    if existing is not None:
        existing_full = await order_repo.get_by_id(existing.id)
        return PlacedOrder(order=existing_full, order_item=existing_full.items[0], delivered_payload=None)

    now = datetime.now(UTC)
    order = Order(
        order_number=new_order_number(),
        user_id=user_id,
        status=OrderStatus.PENDING,
        subtotal_minor=product.price_minor,
        discount_minor=0,
        total_minor=product.price_minor,
        currency=product.currency,
        idempotency_key=idempotency_key,
        placed_at=now,
    )
    session.add(order)
    await session.flush()

    # Link existing hold to this order
    hold = await order_hold_service.get_hold_for_product(session, product.id, user_id)
    if hold is not None:
        hold.order_id = order.id
        await session.flush()

    order_item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        product_name=product.name,
        unit_price_minor=product.price_minor,
        qty=1,
        warranty_days=product.warranty_days,
    )
    session.add(order_item)
    await session.flush()

    delivered_payload: str | None = None

    if product.fulfillment_mode == FulfillmentMode.AUTO:
        claimed = await order_repo.claim_available_stock(product.id, 1)
        if not claimed:
            raise UserError("errors.out_of_stock")
        stock_item = claimed[0]
        stock_item.status = StockStatus.RESERVED
        stock_item.order_item_id = order_item.id
        await session.flush()

        debit_txn = await wallet_service.debit(
            session,
            user_id=user_id,
            amount_minor=product.price_minor,
            currency=product.currency,
            type_=TxnType.PURCHASE,
            idempotency_key=idempotency_key,
            ref_type="order",
            ref_id=order.id,
        )
        order.payment_txn_id = debit_txn.id

        stock_item.status = StockStatus.DELIVERED
        delivered_payload = get_cipher().decrypt(stock_item.payload)

        session.add(
            Delivery(
                order_item_id=order_item.id,
                mode="AUTO",
                payload=None,  # plaintext never persisted a second time; stock_item already holds ciphertext
                delivered_at=now,
            )
        )
        order.status = OrderStatus.COMPLETED
        order.completed_at = now
    else:
        debit_txn = await wallet_service.debit(
            session,
            user_id=user_id,
            amount_minor=product.price_minor,
            currency=product.currency,
            type_=TxnType.PURCHASE,
            idempotency_key=idempotency_key,
            ref_type="order",
            ref_id=order.id,
        )
        order.payment_txn_id = debit_txn.id
        order.status = OrderStatus.PROCESSING

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

    await session.flush()

    if order.status == OrderStatus.COMPLETED:
        buyer = await UserRepo(session).get_by_id(user_id)
        if buyer is not None:
            await referral_service.try_qualify_referral(session, user_id=user_id, referred_by_id=buyer.referred_by_id)

    return PlacedOrder(order=order, order_item=order_item, delivered_payload=delivered_payload)


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
