"""Refunding a warranty claim: money parked, warranty spent, order still delivered.

Driven against the real services on SQLite because all three have to move together — a refund that
parks money but leaves the warranty claimable, or one that cancels the order it was refunding, is
wrong in a way no single assertion catches.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.database.models.order import (
    Order,
    OrderItem,
    OrderStatus,
    RefundState,
    Warranty,
    WarrantyStatus,
)
from app.database.models.user import User
from app.database.models.wallet import TxnType
from app.database.repositories.wallet_repo import WalletRepo
from app.services import order_service, wallet_service
from app.services.warranty_service import now_utc, refund_claim


async def _seed(sessionmaker) -> tuple[int, str, int]:
    """A delivered, paid order with one item under warranty claim."""
    async with sessionmaker() as session:
        user = User(telegram_id=777, username="buyer", referral_code="R2", locale="en", chat_id=777)
        session.add(user)
        await session.flush()

        wallet = await WalletRepo(session).get_or_create(user.id, currency="USD")
        await wallet_service.credit(
            session,
            user_id=user.id,
            amount_minor=1000,
            currency="USD",
            type_=TxnType.TOPUP,
            idempotency_key="seed-topup",
        )
        txn = await wallet_service.debit(
            session,
            user_id=user.id,
            amount_minor=1000,
            currency=wallet.currency,
            type_=TxnType.PURCHASE,
            idempotency_key="seed-purchase",
        )

        now = datetime.now(UTC)
        order = Order(
            order_number="ORD-W1",
            user_id=user.id,
            status=OrderStatus.COMPLETED,
            subtotal_minor=1000,
            total_minor=1000,
            currency="USD",
            idempotency_key="k-warranty-refund",
            placed_at=now,
            completed_at=now,
            payment_txn_id=txn.id,
        )
        session.add(order)
        await session.flush()

        item = OrderItem(
            order_id=order.id,
            product_id=None,
            product_name="Widget",
            unit_price_minor=1000,
            qty=1,
            warranty_days=7,
        )
        session.add(item)
        await session.flush()

        warranty = Warranty(
            order_item_id=item.id,
            user_id=user.id,
            starts_at=now,
            expires_at=now + timedelta(days=7),
            status=WarrantyStatus.CLAIMED,
            claim_started_at=now,
            claim_remaining_seconds=7 * 24 * 3600,
            claim_deadline_at=now + timedelta(days=1),
        )
        session.add(warranty)
        await session.commit()
        return user.id, order.id, warranty.id


async def test_refunding_a_claim_parks_the_money_and_spends_the_warranty(sqlite_sessionmaker) -> None:
    user_id, order_id, warranty_id = await _seed(sqlite_sessionmaker)

    async with sqlite_sessionmaker() as session:
        parked = await order_service.refund_delivered_order(
            session,
            order_id=order_id,
            amount_minor=1000,
            reason="Key stopped working",
            admin_telegram_id=99,
        )
        warranty = await session.get(Warranty, warranty_id)
        refund_claim(warranty, reason="Key stopped working", at=now_utc())
        await session.commit()

    assert parked.amount_minor == 1000

    async with sqlite_sessionmaker() as session:
        order = await session.get(Order, order_id)
        warranty = await session.get(Warranty, warranty_id)
        wallet = await WalletRepo(session).get_or_create(user_id, currency="USD")

        # Delivered stays delivered — the buyer really did receive it.
        assert order.status is OrderStatus.COMPLETED
        assert order.completed_at is not None
        assert order.cancelled_at is None
        assert order.failure_reason is None

        assert order.refund_state is RefundState.PARKED
        assert order.refund_amount_minor == 1000
        # Parked, not spendable: settling it is still a decision somebody has to make.
        assert wallet.refund_balance_minor == 1000
        assert wallet.balance_minor == 0

        # VOID is the one status the claim screen refuses, so nobody can claim it twice.
        assert warranty.status is WarrantyStatus.VOID
        assert warranty.claim_deadline_at is None


async def test_an_order_can_only_be_refunded_once(sqlite_sessionmaker) -> None:
    from app.utils.errors import UserError

    _, order_id, _ = await _seed(sqlite_sessionmaker)

    async with sqlite_sessionmaker() as session:
        await order_service.refund_delivered_order(
            session, order_id=order_id, amount_minor=1000, reason="first", admin_telegram_id=99
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        try:
            await order_service.refund_delivered_order(
                session, order_id=order_id, amount_minor=1000, reason="again", admin_telegram_id=99
            )
        except UserError:
            pass
        else:  # pragma: no cover - the assertion below reports it
            raise AssertionError("a second refund was allowed to park money twice")
