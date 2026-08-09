from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.gift import GiftCode, GiftRedemption


class GiftRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_hash(self, code_hash: str) -> GiftCode | None:
        result = await self._session.execute(select(GiftCode).where(GiftCode.code_hash == code_hash))
        return result.scalar_one_or_none()

    async def get_by_id(self, gift_code_id: int) -> GiftCode | None:
        return await self._session.get(GiftCode, gift_code_id)

    async def list_all(self, limit: int = 30) -> list[GiftCode]:
        result = await self._session.execute(select(GiftCode).order_by(GiftCode.created_at.desc()).limit(limit))
        return list(result.scalars().all())

    async def count_redemptions_for_user(self, gift_code_id: int, user_id: int) -> int:
        from sqlalchemy import func

        result = await self._session.execute(
            select(func.count())
            .select_from(GiftRedemption)
            .where(GiftRedemption.gift_code_id == gift_code_id, GiftRedemption.user_id == user_id)
        )
        return int(result.scalar_one())
