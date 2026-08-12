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
