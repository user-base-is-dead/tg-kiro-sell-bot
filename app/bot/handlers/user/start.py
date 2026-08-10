from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters.is_admin import is_admin_user
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.main_menu import main_inline_keyboard
from app.bot.panel import panel_markup
from app.core.config import get_settings
from app.database.models.user import User
from app.database.repositories.user_repo import UserRepo
from app.locales.i18n import t

logger = logging.getLogger(__name__)

router = Router(name="user.start")


@router.message(CommandStart(deep_link=True))
async def cmd_start_with_ref(message: Message, command: CommandObject, session: AsyncSession, user: User, is_new_user: bool) -> None:
    payload = command.args or ""
    # Only allow referral links for NEW users to prevent existing users from changing their referrer
    if payload.startswith("ref_") and is_new_user and user.referred_by_id is None:
        code = payload.removeprefix("ref_")
        repo = UserRepo(session)
        referrer = await repo.get_by_referral_code(code)
        if referrer is not None and referrer.id != user.id:
            user.referred_by_id = referrer.id
            await session.flush()
    await _send_welcome(message, session, user, force_panel=True)


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, user: User) -> None:
    await _send_welcome(message, session, user, force_panel=True)


@router.message(MenuButton("menu.start"))
async def on_start_button(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    """The panel's Start row. Registered without a state filter and in the first router, so it wins
    over whatever form the user is halfway through — which is the point: it is the one button that
    always works. Clearing the state is part of that; leaving it set would make the user's next
    message get read as an answer to a question they just walked away from."""
    await state.clear()
    await _send_welcome(message, session, user)


async def _send_welcome(
    message: Message, session: AsyncSession, user: User, *, force_panel: bool = False
) -> None:
    locale = user.locale
    is_admin = await is_admin_user(session, user.telegram_id)

    name = user.first_name or "there"

    # Two messages, in this order. The greeting carries the removal of the retired bottom panel
    # (`panel_markup`) — a ReplyKeyboardRemove has to ride on a real send, and it cannot share a
    # message with the inline grid, so the grid gets the second bubble. That order also suits
    # `nav`, which edits the inline message in place.
    #
    # `/start` forces the removal even if this process already sent it: it is the recovery route
    # for a client still showing the old panel.
    await message.answer(
        t("welcome.returning", locale, name=name),
        reply_markup=panel_markup(
            user.telegram_id, locale, is_admin=is_admin, force=force_panel
        ),
    )
    group_url = get_settings().community_group_url.strip()
    body = t("welcome.subtitle", locale, name=name)
    if group_url:
        body += t("welcome.community", locale, group_url=group_url)

    await message.answer(
        body,
        reply_markup=main_inline_keyboard(locale, is_admin=is_admin),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
