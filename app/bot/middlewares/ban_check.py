from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.bot.filters.is_admin import is_admin_user
from app.bot.states.ticket_form import TicketForm
from app.database.models.user import UserStatus
from app.locales.i18n import t

# Commands a suspended account keeps. Support is the whole point — an appeal has to be possible from
# inside the ban — and browsing plus /start are harmless reads that make the bot explainable rather
# than a wall.
_ALLOWED_COMMANDS = {"/start", "/products", "/support", "/mytickets", "/cancel"}

# Reply-panel labels are localized, so they are matched by i18n key rather than by text.
_ALLOWED_MENU_KEYS = ("menu.start", "menu.products", "menu.support")

# Every panel button that is NOT allowed. These have to be named explicitly: a panel press arrives
# as ordinary text, and ordinary text is otherwise let through so it can reach support. Without this
# list, pressing 💳 Top Up while suspended would sail past the middleware and open top-up.
_BLOCKED_MENU_KEYS = (
    "menu.orders",
    "menu.profile",
    "menu.topup",
    "menu.gift",
    "menu.refer",
    "menu.warranty",
    "menu.admin_panel",
)

# Nav targets that only move between the store and the main menu. `cat-<id>` is handled separately
# because it carries an id.
_ALLOWED_NAV_TARGETS = {"home", "categories"}

# FSM states a suspended account may still answer into. Only the support ticket form: everything
# else (top-up amount, gift code, checkout) belongs to an action they are not allowed to take, and
# a state left over from before the ban must not become a way back into it.
_ALLOWED_STATE_GROUP = TicketForm.__name__


def _command_of(message: Message) -> str | None:
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return None
    # `/start deep_link` and `/start@BotName` both have to resolve to `/start`.
    return text.split()[0].split("@")[0].lower()


def _is_allowed_message(message: Message, locale: str, state_name: str | None) -> bool:
    command = _command_of(message)
    if command is not None:
        return command in _ALLOWED_COMMANDS

    if message.text:
        if any(message.text == t(key, locale) for key in _ALLOWED_MENU_KEYS):
            return True
        if any(message.text == t(key, locale) for key in _BLOCKED_MENU_KEYS):
            return False

    # Anything else is free text. It is allowed only when it is going to support: either the ticket
    # form, or the fall-through DM relay that carries replies on an already-open ticket. A different
    # form being open means the text is an answer to something they cannot do.
    return state_name is None or state_name.startswith(_ALLOWED_STATE_GROUP)


def _is_allowed_callback(query: CallbackQuery) -> bool:
    data = query.data or ""
    if data == "noop":
        return True

    prefix, _, rest = data.partition(":")
    if prefix == "sup":  # the whole support surface: open a ticket, list them, read one, close it
        return True
    if prefix == "cat":  # browse a category, page through it
        return True
    if prefix == "prod":
        # Viewing is fine; `prod:buy` is not. This is the one place where a prefix is not enough.
        return rest.split(":")[0] == "view"
    if prefix == "nav":
        target = rest.split(":")[0]
        return target in _ALLOWED_NAV_TARGETS or target.startswith("cat-")
    return False


class BanCheckMiddleware(BaseMiddleware):
    """Cuts a BANNED account down to browsing and support, before any handler runs.

    Registered at the Update level after UserMiddleware (it needs `user` in data).

    Two rules that are easy to get wrong and are therefore enforced here rather than per-handler:

      * **admins are immune.** An admin who somehow carries the BANNED flag — set before admins were
        immune, or by another admin — is still let through. Locking an admin out of the panel takes
        away the only place the ban could be undone;
      * **support always works.** A ban that also silences the appeal channel is a support ticket in
        the making, not moderation. Everything under `sup:`, the ticket form, and plain replies on an
        open ticket stay live.

    Everything else — buying, wallet, top-up, gifts, referrals, orders, the admin panel — is refused
    with the suspension notice.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("user")
        if user is None or user.status != UserStatus.BANNED or not isinstance(event, Update):
            return await handler(event, data)

        if await is_admin_user(data.get("session"), user.telegram_id):
            return await handler(event, data)

        if event.message is not None:
            state = data.get("state")
            state_name = await state.get_state() if state is not None else None
            if _is_allowed_message(event.message, user.locale, state_name):
                return await handler(event, data)
            await event.message.answer(t("common.banned", user.locale))
            return None

        if event.callback_query is not None:
            if _is_allowed_callback(event.callback_query):
                return await handler(event, data)
            await event.callback_query.answer(t("common.banned", user.locale), show_alert=True)
            return None

        return None
