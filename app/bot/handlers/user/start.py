from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters.is_admin import is_admin_user
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.main_menu import main_inline_keyboard
from app.bot.texts import NO_PREVIEW, home_body
from app.database.models.user import User

logger = logging.getLogger(__name__)

router = Router(name="user.start")


# Plain `/start` and `/start <anything>` alike. Invite links (`/start ref_…`) belonged to the removed
# Invite & Earn feature; an old one still opens the bot normally, the payload is simply ignored.
@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, user: User) -> None:
    await _send_welcome(message, session, user)


@router.message(MenuButton("menu.start"))
async def on_start_button(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    """The panel's Start row. Registered without a state filter and in the first router, so it wins
    over whatever form the user is halfway through — which is the point: it is the one button that
    always works. Clearing the state is part of that; leaving it set would make the user's next
    message get read as an answer to a question they just walked away from."""
    await state.clear()
    await _send_welcome(message, session, user)


async def _send_welcome(message: Message, session: AsyncSession, user: User) -> None:
    """One bubble: the home screen and its menu, nothing in front of it.

    There used to be a separate "👋 Welcome" line first, sent only because it was the one message
    that could carry the removal of the retired bottom panel — a ReplyKeyboardRemove cannot share a
    message with an inline grid. Nobody on this bot has that panel, and it read as the bot talking
    twice, so the greeting is gone and the name lives in the home text itself. The language screen
    still takes a stale panel down if one ever turns up (see `set_language`).
    """
    locale = user.locale
    is_admin = await is_admin_user(session, user.telegram_id)
    await message.answer(
        home_body(locale, user.first_name or "there"),
        reply_markup=main_inline_keyboard(locale, is_admin=is_admin),
        link_preview_options=NO_PREVIEW,
    )
