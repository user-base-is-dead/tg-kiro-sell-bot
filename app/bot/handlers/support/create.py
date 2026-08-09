from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import NavCB, SupportCB
from app.bot.filters.is_admin import is_admin_user
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.common import back_keyboard, with_nav
from app.bot.keyboards.main_menu import main_inline_keyboard
from app.bot.keyboards.styles import PRIMARY, btn
from app.bot.states.ticket_form import TicketForm
from app.core.config import get_settings
from app.database.models.user import User
from app.database.repositories.support_repo import SupportRepo
from app.locales.i18n import t
from app.services.support_service import create_ticket

router = Router(name="support.create")

_CATEGORIES = ["General", "Billing", "Technical", "Order Issue"]


def _menu_keyboard(locale: str) -> InlineKeyboardMarkup:
    return with_nav(
        [
            [btn(t("support.create", locale), SupportCB(action="create").pack(), PRIMARY)],
            [btn(t("support.my_tickets", locale), SupportCB(action="mytickets").pack(), PRIMARY)],
        ],
        locale,
        back_target="home",
        home=False,
    )


@router.message(Command("support"))
@router.message(MenuButton("menu.support"))
async def cmd_support(message: Message, user: User) -> None:
    await message.answer(t("support.menu", user.locale), reply_markup=_menu_keyboard(user.locale))


@router.callback_query(NavCB.filter(F.target == "support"))
async def nav_support(query: CallbackQuery, callback_data: NavCB, user: User) -> None:  # noqa: ARG001
    if not query.message:
        return
    await query.message.edit_text(t("support.menu", user.locale), reply_markup=_menu_keyboard(user.locale))
    await query.answer()


@router.callback_query(SupportCB.filter((F.action == "create") & (F.category == "")))
async def start_create(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    existing = await SupportRepo(session).get_open_for_user(user.id)
    if existing is not None:
        await query.answer(f"You already have an open ticket: {existing.ticket_number}. Just reply to chat.", show_alert=True)
        return

    rows = [[btn(cat, SupportCB(action="create", category=cat).pack(), PRIMARY)] for cat in _CATEGORIES]
    await query.message.edit_text(
        t("support.choose_category", user.locale),
        reply_markup=with_nav(rows, user.locale, back_target="support"),
    )
    await query.answer()


@router.callback_query(SupportCB.filter((F.action == "create") & (F.category != "")))
async def pick_category(query: CallbackQuery, callback_data: SupportCB, state: FSMContext, user: User) -> None:
    await state.set_state(TicketForm.subject)
    await state.update_data(category=callback_data.category)
    # Back out of a half-typed ticket without having to remember /cancel — the nav router clears
    # the form state on its way home.
    await query.message.edit_text(
        t("support.ask_subject", user.locale), reply_markup=back_keyboard(user.locale)
    )
    await query.answer()


@router.message(Command("cancel"), TicketForm.subject)
async def cancel_create(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    is_admin = await is_admin_user(session, user.telegram_id)
    await message.answer(
        t("welcome.subtitle", user.locale, name=user.first_name or "there"),
        reply_markup=main_inline_keyboard(user.locale, is_admin=is_admin),
    )


@router.message(TicketForm.subject)
async def receive_subject(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    subject = (message.text or "").strip()
    if not subject:
        await message.answer("Please describe your issue in text, or /cancel:")
        return

    data = await state.get_data()
    await state.clear()

    ticket, reached_staff = await create_ticket(
        message.bot,
        session,
        user=user,
        category=data.get("category", "General"),
        subject=subject,
        support_group_id=get_settings().support_group_id,
    )
    # Never a bare "✅ opened" on the strength of the database row alone — if nobody was reachable,
    # the user is waiting for a reply that is not coming, and has no way to tell.
    key = "support.created" if reached_staff else "support.created_undelivered"
    await message.answer(t(key, user.locale, ticket_number=ticket.ticket_number))
