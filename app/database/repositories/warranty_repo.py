from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.order import OrderItem, Warranty, WarrantyStatus


class WarrantyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, user_id: int, limit: int = 12, offset: int = 0) -> list[Warranty]:
        """Oldest first, so page 1 opens on the customer's earliest purchase and the newest sits at
        the bottom of the last page — the same direction a chat log reads."""
        result = await self._session.execute(
            select(Warranty).where(Warranty.user_id == user_id).order_by(Warranty.id.asc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_user(self, user_id: int) -> int:
        result = await self._session.execute(select(func.count(Warranty.id)).where(Warranty.user_id == user_id))
        return result.scalar() or 0

    async def get_order_item(self, order_item_id: int) -> OrderItem | None:
        return await self._session.get(OrderItem, order_item_id)

    async def get_by_id(self, warranty_id: int) -> Warranty | None:
        return await self._session.get(Warranty, warranty_id)

    async def get_by_ticket_id(self, ticket_id: int) -> Warranty | None:
        result = await self._session.execute(select(Warranty).where(Warranty.claim_ticket_id == ticket_id))
        return result.scalar_one_or_none()

    async def list_claims_past_deadline(self, now: datetime) -> list[Warranty]:
        """Claims whose staff-response window has elapsed. Driven by the stored deadline rather than
        by re-deriving one from `claim_started_at`, so the window is whatever it was when the claim
        was filed even if the configured grace period changes later."""
        result = await self._session.execute(
            select(Warranty).where(
                Warranty.status == WarrantyStatus.CLAIMED,
                Warranty.claim_deadline_at.is_not(None),
                Warranty.claim_deadline_at <= now,
            )
        )
        return list(result.scalars().all())
