from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.settings import BotSetting


class SettingsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> BotSetting | None:
        result = await self._session.execute(select(BotSetting).where(BotSetting.key == key))
        return result.scalar_one_or_none()

    async def set(self, key: str, value_json: str, *, admin_id: int | None) -> BotSetting:
        from datetime import UTC, datetime

        existing = await self.get(key)
        if existing is None:
            existing = BotSetting(key=key, value_json=value_json, updated_by_admin_id=admin_id, updated_at=datetime.now(UTC))
            self._session.add(existing)
        else:
            existing.value_json = value_json
            existing.updated_by_admin_id = admin_id
            existing.updated_at = datetime.now(UTC)
        await self._session.flush()
        return existing
