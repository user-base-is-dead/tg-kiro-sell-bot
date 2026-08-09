from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.main_menu import main_reply_keyboard
from app.bot.states.broadcast_form import BroadcastForm
from app.bot.states.category_form import CategoryForm
from app.bot.states.gift_form import GiftCreateForm
from app.bot.states.order_fulfill_form import OrderFulfillForm
from app.bot.states.product_form import (
    ProductEditForm,
    ProductForm,
    ProductImportForm,
    ProductSearchForm,
    StockUploadForm,
)
from app.bot.states.settings_form import SettingsForm
from app.bot.states.user_search_form import UserBalanceForm, UserSearchForm
from app.database.models.user import User
from app.database.repositories.audit_repo import AuditRepo
from app.locales.i18n import t

logger = logging.getLogger(__name__)

router = Router(name="admin.guard")

# Registered AFTER every admin router, so a real admin's update is already handled and never
# reaches here. What is left is exactly the case we want to be loud about: someone who is not
# an admin invoking an admin command, or replaying/forging an admin callback_data string
# (e.g. copied from a screenshot, or hand-packed as "amisc:settings"). Without this router
# aiogram simply finds no matching handler and the bot stays silent — which reads like a bug
# and tells a prober nothing. We answer "not authorized" and write an audit trail.

# Admin-only commands. /cancel is deliberately absent: it is a shared FSM-exit command that
# non-admin flows use too, and claiming it here would break them.
_ADMIN_COMMANDS = (
    "admin",
    "adjust_balance",
    "dashboard",
    "pending_orders",
    "open_tickets",
    "close",
    "broadcast_status",
    "done",
    "reject",
)

# CallbackData prefixes owned by the admin routers (see app/bot/callbacks.py).
_ADMIN_CB_PREFIXES = frozenset(
    {"acat", "aprod", "aord", "apay", "agift", "atick", "amisc", "auser"}
)

# NavCB targets that only make sense for an admin.
_ADMIN_NAV_TARGETS = frozenset({"admin_panel"})

# FSM wizards that only admins can be inside. Needed for the demotion case: an admin who
# starts a wizard and loses admin mid-flow keeps a live FSM state, but the admin routers now
# reject their next step — the plain-text step would otherwise fall through to the support
# relay catch-all and be sent to staff as a support message.
_ADMIN_STATE_GROUPS = (
    BroadcastForm,
    CategoryForm,
    GiftCreateForm,
    OrderFulfillForm,
    ProductEditForm,
    ProductForm,
    ProductImportForm,
    ProductSearchForm,
    SettingsForm,
    StockUploadForm,
    UserBalanceForm,
    UserSearchForm,
)


def _is_admin_callback(data: str | None) -> bool:
    if not data:
        return False
    head, _, rest = data.partition(":")
    if head in _ADMIN_CB_PREFIXES:
        return True
    return head == "nav" and rest.split(":")[0] in _ADMIN_NAV_TARGETS


async def _record(
    session: AsyncSession | None,
    telegram_id: int,
    attempted: str,
    surface: str,
) -> None:
    logger.warning(
        "Unauthorized admin attempt: telegram_id=%s surface=%s attempted=%r",
        telegram_id,
        surface,
        attempted,
    )
    if session is None:
        return
    await AuditRepo(session).log(
        actor_telegram_id=telegram_id,
        action="security.unauthorized_admin",
        target_type=surface,
        target_id=attempted[:64],
        metadata={"attempted": attempted[:256], "surface": surface},
    )


@router.message(Command(*_ADMIN_COMMANDS))
@router.message(MenuButton("menu.admin_panel"))
async def deny_admin_message(
    message: Message,
    session: AsyncSession | None = None,
    user: User | None = None,
) -> None:
    locale = user.locale if user else "en"
    telegram_id = user.telegram_id if user else (message.from_user.id if message.from_user else 0)
    await _record(session, telegram_id, (message.text or "")[:256], "command")

    # A reply keyboard lives on the client until it is replaced, so a demoted admin keeps seeing
    # the 🛡️ row until something re-sends the panel. If that stale row is what they just pressed,
    # replace it here instead of making them find their way to /start.
    markup = None
    if message.text == t("menu.admin_panel", locale):
        markup = main_reply_keyboard(locale, is_admin=False)

    await message.answer(t("common.unauthorized", locale), reply_markup=markup)


@router.message(StateFilter(*_ADMIN_STATE_GROUPS))
async def deny_admin_form_step(
    message: Message,
    state: FSMContext,
    session: AsyncSession | None = None,
    user: User | None = None,
) -> None:
    locale = user.locale if user else "en"
    telegram_id = user.telegram_id if user else (message.from_user.id if message.from_user else 0)
    current = await state.get_state()
    await _record(session, telegram_id, current or "", "fsm_state")
    await state.clear()  # drop the half-built wizard so their next message is an ordinary one
    await message.answer(t("common.unauthorized", locale))


@router.callback_query(lambda query: _is_admin_callback(query.data))
async def deny_admin_callback(
    query: CallbackQuery,
    session: AsyncSession | None = None,
    user: User | None = None,
) -> None:
    locale = user.locale if user else "en"
    telegram_id = user.telegram_id if user else query.from_user.id
    await _record(session, telegram_id, query.data or "", "callback")
    # show_alert so a forged inline button can't be mistaken for a no-op tap.
    await query.answer(t("common.unauthorized", locale), show_alert=True)
