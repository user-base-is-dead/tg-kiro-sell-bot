from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import LangCB, NavCB
from app.bot.filters.is_admin import is_admin_user
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.main_menu import language_inline_keyboard, main_inline_keyboard
from app.bot.panel import panel_markup
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
            t("welcome.menu_prompt", locale),
            reply_markup=main_inline_keyboard(locale, is_admin=is_admin),
        )
        # The panel's buttons send their own localized label as plain text, so a locale change must
        # replace them or the `MenuButton` filter stops matching. The new locale is part of the
        # install key, so this genuinely re-issues rather than hitting the cache.
        #
        # The edit above cannot carry it (edits take inline markup only), so the new panel rides on
        # the confirmation line — a message worth showing on its own, and one that stays put. It
        # must stay: deleting a reply keyboard's message takes the panel down on mobile.
        markup = panel_markup(user.telegram_id, locale, is_admin=is_admin)
        if markup is not None:
            await query.message.answer(t("language.changed", locale), reply_markup=markup)
