from __future__ import annotations

from typing import Any

from aiogram.filters import Filter
from aiogram.types import TelegramObject

from app.core.config import get_settings
from app.database.repositories.admin_repo import AdminRepo


async def _resolve_telegram_id(event: TelegramObject) -> int | None:
    if hasattr(event, "from_user") and event.from_user is not None:
        return event.from_user.id
    return None


async def is_admin_user(session: Any, telegram_id: int) -> bool:
    """Plain-function version of the IsAdmin check, for UI decisions (e.g. which reply-keyboard
    rows to render) that need the answer without going through the filter machinery."""
    settings = get_settings()
    if telegram_id in settings.admin_ids:
        return True
    if session is None:
        return False
    admin = await AdminRepo(session).get_by_telegram_id(telegram_id)
    return admin is not None


class IsAdmin(Filter):
    """Never inferred from a hidden button or a callback_data flag. Every admin-gated handler
    (command AND every admin component/callback) applies this independently."""

    async def __call__(self, event: TelegramObject, **data: Any) -> bool:
        telegram_id = await _resolve_telegram_id(event)
        if telegram_id is None:
            return False

        settings = get_settings()
        if telegram_id in settings.admin_ids:
            return True

        session = data.get("session")
        if session is None:
            return False

        admin = await AdminRepo(session).get_by_telegram_id(telegram_id)
        return admin is not None


class IsOwner(Filter):
    async def __call__(self, event: TelegramObject, **data: Any) -> bool:
        telegram_id = await _resolve_telegram_id(event)
        if telegram_id is None:
            return False

        settings = get_settings()
        if telegram_id in settings.admin_ids:
            return True

        session = data.get("session")
        if session is None:
            return False

        from app.database.models.admin import AdminRole

        admin = await AdminRepo(session).get_by_telegram_id(telegram_id)
        return admin is not None and admin.role == AdminRole.OWNER
