from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.database.repositories.user_repo import UserRepo


class UserMiddleware(BaseMiddleware):
    """Upserts the User row from the raw Telegram event, caches chat_id, injects `user`.
    Runs after DbSessionMiddleware (needs `session` in data already)."""

    def __init__(self, default_locale: str = "en") -> None:
        self._default_locale = default_locale

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = None
        chat_id = None

        if isinstance(event, Update):
            if event.message:
                from_user = event.message.from_user
                if event.message.chat.type == "private":
                    chat_id = event.message.chat.id
            elif event.callback_query:
                from_user = event.callback_query.from_user
                if event.callback_query.message and event.callback_query.message.chat.type == "private":
                    chat_id = event.callback_query.message.chat.id

        if from_user is None or from_user.is_bot:
            return await handler(event, data)

        repo = UserRepo(data["session"])
        user, is_new_user = await repo.upsert_from_telegram(
            telegram_id=from_user.id,
            username=from_user.username,
            first_name=from_user.first_name,
            last_name=from_user.last_name,
            chat_id=chat_id,
            default_locale=self._default_locale,
        )
        data["user"] = user
        data["is_new_user"] = is_new_user

        return await handler(event, data)
