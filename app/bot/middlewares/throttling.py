from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from redis.asyncio import Redis

from app.locales.i18n import t


class ThrottlingMiddleware(BaseMiddleware):
    """Fixed-window rate limit per user, via Redis INCR+EXPIRE (works across process restarts
    and multiple bot workers). Silently drops the update past the limit with one warning,
    then hard-drops until the window rolls over — never crashes the handler chain."""

    def __init__(self, redis: Redis, *, limit: int = 20, window_seconds: int = 10) -> None:
        self._redis = redis
        self._limit = limit
        self._window = window_seconds

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)

        from_user = None
        if event.message:
            from_user = event.message.from_user
        elif event.callback_query:
            from_user = event.callback_query.from_user

        if from_user is None or from_user.is_bot:
            return await handler(event, data)

        key = f"throttle:{from_user.id}"
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, self._window)

        if count > self._limit:
            if count == self._limit + 1:
                text = t("common.flood", "en")
                if event.message:
                    await event.message.answer(text)
                elif event.callback_query:
                    await event.callback_query.answer(text, show_alert=True)
            return None

        return await handler(event, data)
