from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.repositories.settings_repo import SettingsRepo

# BotSetting is DB-backed, not env-backed, so admins can tune it at runtime without a
# redeploy. No in-process cache yet (traffic doesn't warrant it) — add one here first if it
# ever becomes a hot path.

DEFAULTS: dict[str, Any] = {
    "referral_reward_minor": 500,  # 5.00 in default currency
    "referral_qualify_on": "first_completed_order",
}


async def get_setting(session: AsyncSession, key: str) -> Any:
    row = await SettingsRepo(session).get(key)
    if row is None:
        return DEFAULTS.get(key)
    return json.loads(row.value_json)


async def set_setting(session: AsyncSession, key: str, value: Any, *, admin_id: int | None) -> None:
    await SettingsRepo(session).set(key, json.dumps(value), admin_id=admin_id)
