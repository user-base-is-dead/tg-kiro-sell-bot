from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters.is_admin import is_admin_user
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.main_menu import main_inline_keyboard
from app.bot.panel import panel_markup
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

    # Two messages, in this order, and neither is a throwaway. The greeting carries the bottom
    # panel because the message a reply keyboard arrives on has to stay in the chat — deleting it
    # takes the panel down on mobile. The inline grid then needs its own bubble (one send cannot
    # hold both markups) and it must be the *second* one, because `nav` edits it in place and a
    # reply-keyboard message cannot be edited into an inline one. See `panel_markup`.
    #
    # `/start` forces the panel: it is the only way a user whose client dropped the keyboard can
    # ask for it back. The panel's own Start button leaves `force_panel` false — whoever pressed
    # it plainly still has the panel.
    await message.answer(
        t("welcome.returning", locale, name=name),
        reply_markup=panel_markup(
            user.telegram_id, locale, is_admin=is_admin, force=force_panel
        ),
    )
    await message.answer(
        t("welcome.subtitle", locale, name=name),
        reply_markup=main_inline_keyboard(locale, is_admin=is_admin),
    )
