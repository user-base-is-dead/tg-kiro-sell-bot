from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.interaction_state import InteractionState
from app.utils.time import as_utc


class InteractionStateRepo:
    """Overflow storage for callback_data payloads that would exceed Telegram's 64-byte
    limit, and for wizard state that must survive an FSM reset. Referenced from callback_data
    as a short opaque token — never trust the token's *content* without a DB round-trip."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, user_id: int, payload: dict[str, Any], *, ttl_seconds: int = 3600) -> str:
        token = secrets.token_urlsafe(9)[:12]
        state = InteractionState(
            id=token,
            user_id=user_id,
            payload_json=json.dumps(payload),
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )
        self._session.add(state)
        await self._session.flush()
        return token

    async def get(self, token: str) -> dict[str, Any] | None:
        result = await self._session.execute(select(InteractionState).where(InteractionState.id == token))
        state = result.scalar_one_or_none()
        if state is None:
            return None
        # `as_utc`: SQLite returns this naive, and comparing it to an aware `now` raises.
        if as_utc(state.expires_at) < datetime.now(UTC):
            return None
        return json.loads(state.payload_json)

    async def purge_expired(self) -> None:
        await self._session.execute(delete(InteractionState).where(InteractionState.expires_at < datetime.now(UTC)))
