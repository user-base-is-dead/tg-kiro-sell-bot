from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.order import OrderItem, Warranty, WarrantyStatus


class WarrantyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, user_id: int, limit: int = 12, offset: int = 0) -> list[Warranty]:
        result = await self._session.execute(
            select(Warranty).where(Warranty.user_id == user_id).order_by(Warranty.id.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_user(self, user_id: int) -> int:
        from sqlalchemy import func
        result = await self._session.execute(
            select(func.count(Warranty.id)).where(Warranty.user_id == user_id)
        )
        return result.scalar() or 0

    async def get_order_item(self, order_item_id: int) -> OrderItem | None:
        return await self._session.get(OrderItem, order_item_id)

    async def get_by_id(self, warranty_id: int) -> Warranty | None:
        return await self._session.get(Warranty, warranty_id)

    async def get_by_ticket_id(self, ticket_id: int) -> Warranty | None:
        result = await self._session.execute(
            select(Warranty).where(Warranty.claim_ticket_id == ticket_id)
        )
        return result.scalar_one_or_none()

    async def list_pending_claims(self) -> list[Warranty]:
        """Get all warranty claims that haven't been responded to within 24 hours."""
        now = datetime.now(UTC)
        cutoff_time = now - timedelta(hours=24)
        result = await self._session.execute(
            select(Warranty).where(
                (Warranty.status == WarrantyStatus.CLAIMED) &
                (Warranty.claim_started_at <= cutoff_time)
            )
        )
        return list(result.scalars().all())
