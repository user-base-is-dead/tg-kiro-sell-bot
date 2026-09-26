from __future__ import annotations

import logging
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import TelegramObject, Update

from app.core.config import get_settings
from app.locales.i18n import t
from app.utils.errors import UserError
from app.utils.text import escape_html

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
            if self._is_admin(data):
                await self._notify_admin(event, exc)
            else:
                await self._notify_user(event, data, "common.session_expired")
        except TelegramAPIError as exc:
            logger.exception("Telegram API error: %s", exc)
        except UserError as exc:
            await self._notify_user(event, data, exc.i18n_key, **exc.vars)
        except Exception as exc:
            logger.exception("Unhandled error while processing update")
            # An admin gets the real reason on screen. The generic alert is right for a shopper, but
            # for the person who has to fix it, it hides the one fact that matters — and the log it
            # points at needs SSH access to read.
            if self._is_admin(data):
                await self._notify_admin(event, exc)
            else:
                await self._notify_user(event, data, "common.error_generic")
            await self._report(exc)
        return None

    @staticmethod
    def _is_admin(data: dict[str, Any]) -> bool:
        """Admins by env, read without the database — the session is already rolled back here."""
        user = data.get("event_from_user")
        if user is None:
            return False
        try:
            return user.id in get_settings().admin_ids
        except Exception:  # noqa: BLE001 - the notification must survive a bad config
            return False

    async def _notify_admin(self, event: TelegramObject, exc: Exception) -> None:
        detail = f"{type(exc).__name__}: {exc}"
        text = "⚠️ <b>Error (admin view)</b>\n<code>" + escape_html(detail[:600]) + "</code>"
        if not isinstance(event, Update):
            return
        try:
            if event.message:
                await event.message.answer(text)
            elif event.callback_query:
                # Alerts are plain text and capped around 200 chars, so the detail goes in a
                # message and the alert only says where to look.
                await event.callback_query.answer(detail[:190], show_alert=True)
        except TelegramAPIError:
            pass

    async def _report(self, exc: Exception) -> None:
        """Put the traceback where an admin can see it, if a log chat is configured."""
        if not self._log_chat_id:
            return
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        body = escape_html(trace[-3000:])
        try:
            await self._bot.send_message(
                self._log_chat_id, f"⚠️ <b>Unhandled error</b>\n<pre>{body}</pre>"
            )
        except TelegramAPIError:
            pass

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
