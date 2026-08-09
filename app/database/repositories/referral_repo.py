from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.referral import Referral


class ReferralRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_referee(self, referee_id: int) -> Referral | None:
        result = await self._session.execute(select(Referral).where(Referral.referee_id == referee_id))
        return result.scalar_one_or_none()

    async def count_for_referrer(self, referrer_id: int) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(Referral).where(Referral.referrer_id == referrer_id)
        )
        return int(result.scalar_one())

    async def count_qualified_for_referrer(self, referrer_id: int) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(Referral)
            .where(Referral.referrer_id == referrer_id, Referral.qualified_at.is_not(None))
        )
        return int(result.scalar_one())
