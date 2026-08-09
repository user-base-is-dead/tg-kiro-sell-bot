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
from app.bot.keyboards.common import back_keyboard
from app.bot.keyboards.main_menu import main_inline_keyboard, main_reply_keyboard
from app.core.config import get_settings
from app.database.models.order import OrderStatus
from app.database.models.user import User
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.locales.i18n import t
from app.utils.money import format_minor

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
        await query.message.edit_text(
            t("welcome.subtitle", user.locale, name=user.first_name or "there"),
            reply_markup=main_inline_keyboard(user.locale, is_admin=is_admin),
        )
        try:
            reply_kb = main_reply_keyboard(user.locale, is_admin=is_admin)
            await query.message.answer(" ", reply_markup=reply_kb)
            logger.debug("Reply keyboard sent on home navigation")
        except Exception as e:
            logger.error("Failed to send reply keyboard on home navigation: %s", e, exc_info=True)
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
        wallet = await WalletRepo(session).get_or_create(user.id, currency=get_settings().default_currency)
        order_repo = OrderRepo(session)
        total_orders = await order_repo.count_for_user(user.id)

        recent = await order_repo.list_for_user(user.id, offset=0, limit=100)
        total_spent = sum(o.total_minor for o in recent if o.status == OrderStatus.COMPLETED)

        username = f"@{user.username}" if user.username else "—"
        text = (
            "━━━━━━━━━━━━━━━━━━\n"
            "👤 <b>MY PROFILE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"Username: {username}\n"
            f"ID: <code>{user.telegram_id}</code>\n\n"
            f"📦 Orders: {total_orders}\n"
            f"💰 Total Spent: {format_minor(total_spent, wallet.currency)}\n"
            f"💳 Balance: {format_minor(wallet.balance_minor, wallet.currency)}"
        )
        await query.message.edit_text(text, reply_markup=back_keyboard(user.locale))
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
