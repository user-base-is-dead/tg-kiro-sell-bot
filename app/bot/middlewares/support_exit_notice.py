from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, TelegramObject

from app.bot.handlers.support.relay import dm_relay
from app.database.repositories.support_repo import SupportRepo
from app.locales.i18n import t

logger = logging.getLogger(__name__)

# Stored in the user's FSM data. A wizard's `state.clear()` also drops it, which at worst costs one
# extra notice — cheap, and far better than a second storage backend just for one boolean.
_NOTIFIED = "support_exit_notified"


class SupportExitNoticeMiddleware(BaseMiddleware):
    """Tells a user with a live ticket when the thing they just sent did *not* go to support.

    Registered as an inner middleware on the message observer, so aiogram has already picked the
    handler by the time this runs and hands it over in `data["handler"]`. That is the whole point:
    "was this relayed?" is answered by the routing decision itself rather than by re-deriving the
    relay's own conditions here, which would drift the first time either side changed.

    Fires once per departure. The notice repeats only after the user has actually reached support
    again, so browsing six menu screens in a row produces one message, not six.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        result = await handler(event, data)
        try:
            await self._maybe_notify(event, data)
        except TelegramAPIError as exc:
            # Never let the notice take down the handler's own work, which has already succeeded.
            logger.warning("Couldn't deliver support-exit notice (%s)", exc)
        return result

    async def _maybe_notify(self, event: TelegramObject, data: dict[str, Any]) -> None:
        if not isinstance(event, Message) or event.chat.type != "private":
            return

        user = data.get("user")
        session = data.get("session")
        state = data.get("state")
        if user is None or session is None or state is None:
            return

        matched = data.get("handler")
        already_notified = (await state.get_data()).get(_NOTIFIED, False)

        if matched is not None and matched.callback is dm_relay:
            # They are talking to support again — arm the notice for their next departure.
            if already_notified:
                await state.update_data(**{_NOTIFIED: False})
            return

        if already_notified:
            return

        ticket = await SupportRepo(session).get_open_for_user(user.id)
        if ticket is None:
            return

        await state.update_data(**{_NOTIFIED: True})
        await event.answer(t("support.left_conversation", user.locale, ticket_number=ticket.ticket_number))
