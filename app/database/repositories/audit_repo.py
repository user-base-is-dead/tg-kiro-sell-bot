from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.audit import AuditLog


class AuditRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log(
        self,
        *,
        actor_telegram_id: int,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        context: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            actor_telegram_id=actor_telegram_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata_json=json.dumps(metadata) if metadata else None,
            context=context,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def list_recent(self, limit: int = 50) -> list[AuditLog]:
        result = await self._session.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))
        return list(result.scalars().all())
