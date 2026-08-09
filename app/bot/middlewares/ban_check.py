from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.database.models.user import UserStatus
from app.locales.i18n import t


class BanCheckMiddleware(BaseMiddleware):
    """Short-circuits BANNED users before any handler runs. Registered at the Update level,
    after UserMiddleware (needs `user` already in data)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("user")
        if user is not None and user.status == UserStatus.BANNED and isinstance(event, Update):
            text = t("common.banned", user.locale)
            if event.message:
                await event.message.answer(text)
            elif event.callback_query:
                await event.callback_query.answer(text, show_alert=True)
            return None

        return await handler(event, data)
