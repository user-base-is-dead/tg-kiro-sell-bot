from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models.catalog import StockItem, StockStatus
from app.database.models.order import Order


class OrderRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(self, key: str) -> Order | None:
        result = await self._session.execute(select(Order).where(Order.idempotency_key == key))
        return result.scalar_one_or_none()

    async def get_by_id(self, order_id: str) -> Order | None:
        result = await self._session.execute(
            select(Order).where(Order.id == order_id).options(selectinload(Order.items))
        )
        return result.scalar_one_or_none()

    async def list_for_user(self, user_id: int, *, offset: int = 0, limit: int = 8) -> list[Order]:
        result = await self._session.execute(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.placed_at.desc())
            .offset(offset)
            .limit(limit)
            .options(selectinload(Order.items))
        )
        return list(result.scalars().all())

    async def count_for_user(self, user_id: int) -> int:
        result = await self._session.execute(select(func.count()).select_from(Order).where(Order.user_id == user_id))
        return int(result.scalar_one())

    async def count_completed_for_user(self, user_id: int) -> int:
        from app.database.models.order import OrderStatus

        result = await self._session.execute(
            select(func.count()).select_from(Order).where(Order.user_id == user_id, Order.status == OrderStatus.COMPLETED)
        )
        return int(result.scalar_one())

    async def get_by_order_number(self, order_number: str) -> Order | None:
        """Case-insensitive so an admin typing `ord-4a9c` on a phone keyboard finds the order."""
        result = await self._session.execute(
            select(Order)
            .where(func.upper(Order.order_number) == order_number.strip().upper())
            .options(selectinload(Order.items))
        )
        return result.scalars().first()

    async def search(self, term: str, *, limit: int = 10) -> list[Order]:
        """Find orders by anything an admin might have in front of them.

        Four things resolve here, in the order somebody is most likely to paste one: an event ID
        (`DEC-1A0F73`, `RFD-…` — the handle a refund conversation quotes), a full or partial order
        number, an order UUID, and a ticket number. A bare `4A9C` is treated as a partial order
        number, because that is the half of `ORD-4A9C` people actually read out.

        Anything unrecognised returns empty rather than raising: the caller's job is to say "no
        match", and a lookup that throws on a typo turns a search box into a minefield.
        """
        from app.database.models.order_event import OrderEvent
        from app.database.models.support import SupportTicket

        term = term.strip()
        if not term:
            return []
        upper = term.upper()

        # An event ID is unique and points at exactly one order, so it short-circuits everything.
        event_order = await self._session.execute(
            select(OrderEvent.order_id).where(func.upper(OrderEvent.event_number) == upper)
        )
        order_id = event_order.scalars().first()
        if order_id is not None:
            found = await self.get_by_id(order_id)
            return [found] if found is not None else []

        # A ticket number reaches the order it is settling. Same reasoning: one ticket, one order.
        if upper.startswith("TCK-"):
            ticket = await self._session.execute(
                select(SupportTicket.id).where(func.upper(SupportTicket.ticket_number) == upper)
            )
            ticket_id = ticket.scalars().first()
            if ticket_id is None:
                return []
            by_ticket = await self._session.execute(
                select(Order)
                .where(Order.refund_ticket_id == ticket_id)
                .options(selectinload(Order.items))
                .limit(limit)
            )
            return list(by_ticket.scalars().all())

        result = await self._session.execute(
            select(Order)
            .where(
                or_(
                    func.upper(Order.order_number).like(f"%{upper}%"),
                    Order.id == term,
                )
            )
            .order_by(Order.placed_at.desc())
            .options(selectinload(Order.items))
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_all(self, *, offset: int = 0, limit: int = 10) -> list[Order]:
        """Every order, newest first — the browsable counterpart to `search`.

        The pending-fulfilment queue is what an admin needs most days, but it cannot show a cancelled
        or completed order at all, which made history reachable only by knowing an ID in advance.
        """
        result = await self._session.execute(
            select(Order)
            .order_by(Order.placed_at.desc(), Order.id.desc())
            .offset(offset)
            .limit(limit)
            .options(selectinload(Order.items))
        )
        return list(result.scalars().all())

    async def count_all(self) -> int:
        result = await self._session.execute(select(func.count()).select_from(Order))
        return int(result.scalar_one())

    async def list_refund_parked(self, *, limit: int = 50) -> list[Order]:
        """Declined orders whose money is sitting in a Refund Wallet, unsettled.

        This is the queue behind the admin panel's Refund Wallets screen: money owed to somebody that
        no human has accounted for yet. Newest first, because the oldest ones are usually the ones
        already being discussed in a ticket.
        """
        from app.database.models.order import RefundState

        result = await self._session.execute(
            select(Order)
            .where(Order.refund_state == RefundState.PARKED)
            .order_by(Order.cancelled_at.desc(), Order.id.desc())
            .limit(limit)
            .options(selectinload(Order.items))
        )
        return list(result.scalars().all())

    async def list_refunded_for_user(self, user_id: int, *, limit: int = 20) -> list[Order]:
        """Every order that ever put money in this user's Refund Wallet, settled or not — the
        provenance list on the settle screen, so an admin can see what the balance is made of."""
        from app.database.models.order import RefundState

        result = await self._session.execute(
            select(Order)
            .where(Order.user_id == user_id, Order.refund_state != RefundState.NONE)
            .order_by(Order.cancelled_at.desc(), Order.id.desc())
            .limit(limit)
            .options(selectinload(Order.items))
        )
        return list(result.scalars().all())

    async def list_pending_manual(self, *, offset: int = 0, limit: int = 10) -> list[Order]:
        """AUTO orders resolve straight to COMPLETED; PROCESSING only ever means "awaiting
        manual fulfillment by an admin"."""
        from app.database.models.order import OrderStatus

        stmt = (
            select(Order)
            .where(Order.status == OrderStatus.PROCESSING)
            .order_by(Order.placed_at)
            .offset(offset)
            .limit(limit)
            .options(selectinload(Order.items))
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def claim_available_stock(self, product_id: int, qty: int) -> list[StockItem]:
        """SELECT ... FOR UPDATE SKIP LOCKED — the hot path that makes overselling structurally
        impossible instead of merely unlikely under concurrent buyers.

        A credential HELD by someone whose window has already closed counts as claimable; one still
        inside its window never does, which is what keeps a held login from reaching a second
        customer. The caller flips the row to RESERVED while still holding the lock.
        """
        now = datetime.now(UTC)
        stmt = (
            select(StockItem)
            .where(
                StockItem.product_id == product_id,
                or_(
                    StockItem.status == StockStatus.AVAILABLE,
                    and_(StockItem.status == StockStatus.HELD, StockItem.held_until <= now),
                ),
            )
            .limit(qty)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
