from __future__ import annotations

import logging

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import NavCB
from app.bot.filters.is_admin import is_admin_user
from app.bot.handlers.orders.history import render_history
from app.bot.handlers.products.browse import render_categories, render_product_list
from app.bot.handlers.user.profile import render_profile_screen
from app.bot.handlers.user.refunds import render_refunds
from app.bot.keyboards.main_menu import main_inline_keyboard
from app.bot.texts import NO_PREVIEW, home_body
from app.database.models.user import User
from app.locales.i18n import t

logger = logging.getLogger(__name__)

router = Router(name="nav")

# Single place that resolves every `[ 🔙 Back ]` / `[ 🏠 Home ]` press, regardless of which
# domain rendered the screen. Each domain module only needs to add its own `elif target...`
# branch here (or, once there are many, split into a small per-domain registry) rather than
# each registering its own NavCB.filter() handler and racing for the same callback prefix.


@router.callback_query(lambda c: c.data == "noop")
async def on_noop(query: CallbackQuery) -> None:
    await query.answer()


@router.callback_query(NavCB.filter())
async def on_nav(
    query: CallbackQuery, callback_data: NavCB, state: FSMContext, session: AsyncSession, user: User
) -> None:
    if not query.message:
        return

    target = callback_data.target

    if target == "home":
        # Back/Home is the documented way out of a half-finished form (top-up, gift code, ticket
        # subject), so it has to drop the FSM state as well — otherwise the user lands on the menu
        # but their next message is still swallowed by the form they thought they left.
        await state.clear()
        is_admin = await is_admin_user(session, user.telegram_id)
        logger.debug("Navigating to home with is_admin=%s", is_admin)
        # No panel re-send here. The reply keyboard is `is_persistent=True`, so it is still on the
        # client from `/start`; re-installing it would need a fresh message (edits cannot carry a
        # reply keyboard) and that extra bubble is exactly what we refuse to put in the chat.
        await query.message.edit_text(
            home_body(user.locale, user.first_name),
            reply_markup=main_inline_keyboard(user.locale, is_admin=is_admin),
            link_preview_options=NO_PREVIEW,
        )
        await query.answer()
        return

    if target == "categories":
        text, markup = await render_categories(session, user.locale)
        await query.message.edit_text(text, reply_markup=markup)
        await query.answer()
        return

    if target == "orders":
        text, markup = await render_history(session, user.id, 1, user.locale)
        await query.message.edit_text(text, reply_markup=markup)
        await query.answer()
        return

    if target == "profile":
        text, markup = await render_profile_screen(session, user)
        await query.message.edit_text(text, reply_markup=markup)
        await query.answer()
        return

    if target == "refunds":
        text, markup = await render_refunds(session, user)
        await query.message.edit_text(text, reply_markup=markup)
        await query.answer()
        return

    if target.startswith("cat-"):
        category_id = int(target.removeprefix("cat-"))
        rendered = await render_product_list(session, category_id, 1, user.locale)
        if rendered is None:
            await query.answer(t("common.unknown_action", user.locale), show_alert=True)
            return
        text, markup = rendered
        await query.message.edit_text(text, reply_markup=markup)
        await query.answer()
        return

    await query.answer(t("common.unknown_action", user.locale), show_alert=True)
