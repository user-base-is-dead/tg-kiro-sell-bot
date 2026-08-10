from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import TelegramObject, Update

from app.locales.i18n import t
from app.utils.errors import UserError

logger = logging.getLogger(__name__)

DEFAULT_LOCALE = "en"


def _locale_of(user: Any) -> str:
    """Read the user's locale without ever raising.

    This middleware is outermost, so by the time it handles an exception the DbSession middleware
    has already rolled back and closed the session — which expires the `User` and turns a plain
    attribute read into a lazy refresh that raises DetachedInstanceError. That exception would
    escape the error handler itself, so the user gets no message at all and the original traceback
    is buried under a confusing second one. A slightly-wrong language is a much better failure than
    silence."""
    if user is None:
        return DEFAULT_LOCALE
    try:
        return user.locale or DEFAULT_LOCALE
    except Exception:  # noqa: BLE001 - the notification must survive any ORM state
        return DEFAULT_LOCALE


class ErrorMiddleware(BaseMiddleware):
    """Outermost middleware. Catches everything so the poller never dies from a handler bug.
    UserError -> friendly localized message. Anything else -> generic message to the user,
    full trace to logs (+ optional log chat)."""

    def __init__(self, bot: Bot, log_chat_id: int | None = None) -> None:
        self._bot = bot
        self._log_chat_id = log_chat_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramBadRequest as exc:
            if "query is too old" in str(exc).lower() or "message is not modified" in str(exc).lower():
                return None
            logger.warning("Telegram API bad request: %s", exc)
            await self._notify_user(event, data, "common.session_expired")
        except TelegramAPIError as exc:
            logger.exception("Telegram API error: %s", exc)
        except UserError as exc:
            await self._notify_user(event, data, exc.i18n_key, **exc.vars)
        except Exception:
            logger.exception("Unhandled error while processing update")
            await self._notify_user(event, data, "common.error_generic")
            if self._log_chat_id:
                try:
                    await self._bot.send_message(self._log_chat_id, "⚠️ Unhandled error — check logs.")
                except TelegramAPIError:
                    pass
        return None

    async def _notify_user(self, event: TelegramObject, data: dict[str, Any], key: str, **vars: Any) -> None:
        if not isinstance(event, Update):
            return
        text = t(key, _locale_of(data.get("user")), **vars)
        try:
            if event.message:
                await event.message.answer(text)
            elif event.callback_query:
                await event.callback_query.answer(text, show_alert=True)
        except TelegramAPIError:
            pass
