from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import LangCB, NavCB
from app.bot.filters.is_admin import is_admin_user
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.main_menu import (
    language_inline_keyboard,
    main_inline_keyboard,
    main_reply_keyboard,
)
from app.database.models.user import User
from app.locales.i18n import supported_locales, t

logger = logging.getLogger(__name__)

router = Router(name="user.language")


@router.message(Command("language"))
@router.message(MenuButton("menu.language"))
async def show_language_picker(message: Message, user: User) -> None:
    await message.answer(
        t("language.prompt", user.locale), reply_markup=language_inline_keyboard(user.locale)
    )


@router.callback_query(NavCB.filter(F.target == "language"))
async def nav_language(query: CallbackQuery, callback_data: NavCB, user: User) -> None:  # noqa: ARG001
    if not query.message:
        return
    await query.message.edit_text(
        t("language.prompt", user.locale), reply_markup=language_inline_keyboard(user.locale)
    )
    await query.answer()


@router.callback_query(LangCB.filter())
async def set_language(query: CallbackQuery, callback_data: LangCB, session: AsyncSession, user: User) -> None:
    locale = callback_data.locale if callback_data.locale in supported_locales() else "en"
    user.locale = locale
    await session.flush()

    is_admin = await is_admin_user(session, user.telegram_id)
    await query.answer(t("language.changed", locale))
    if query.message:
        logger.debug("Language changed to %s, updating keyboards", locale)
        await query.message.edit_text(
            t("welcome.subtitle", locale), reply_markup=main_inline_keyboard(locale, is_admin=is_admin)
        )
        try:
            reply_kb = main_reply_keyboard(locale, is_admin=is_admin)
            await query.message.answer(" ", reply_markup=reply_kb)
            logger.debug("Reply keyboard sent with new language")
        except Exception as e:
            logger.error("Failed to send reply keyboard after language change: %s", e, exc_info=True)
