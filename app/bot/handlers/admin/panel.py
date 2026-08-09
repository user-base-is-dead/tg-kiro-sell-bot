from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.callbacks import (
    AdminCategoryCB,
    AdminGiftCB,
    AdminMiscCB,
    AdminOrderCB,
    AdminPaymentCB,
    AdminProductCB,
    NavCB,
)
from app.bot.filters.is_admin import IsAdmin
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.common import with_nav
from app.bot.keyboards.styles import PRIMARY, btn
from app.database.models.user import User

router = Router(name="admin.panel")
router.message.filter(IsAdmin())  # every handler in this router independently requires admin


def _panel_keyboard(locale: str) -> InlineKeyboardMarkup:
    return with_nav(
        [
            [btn("📊 Dashboard", AdminMiscCB(action="dashboard").pack(), PRIMARY)],
            [
                btn("📦 Products", AdminProductCB(action="list").pack(), PRIMARY),
                btn("📁 Categories", AdminCategoryCB(action="list").pack(), PRIMARY),
            ],
            [
                btn("👥 Users", AdminMiscCB(action="users").pack(), PRIMARY),
                btn("🛒 Orders", AdminOrderCB(action="list").pack(), PRIMARY),
            ],
            [
                btn("💰 Payments", AdminPaymentCB(action="list").pack(), PRIMARY),
                btn("🎁 Gift Codes", AdminGiftCB(action="list").pack(), PRIMARY),
            ],
            [
                btn("📢 Broadcast", AdminMiscCB(action="broadcast").pack(), PRIMARY),
                btn("⚙️ Settings", AdminMiscCB(action="settings").pack(), PRIMARY),
            ],
            [btn("📝 Logs", AdminMiscCB(action="logs").pack(), PRIMARY)],
        ],
        locale,
        back_target="home",
        home=False,
    )


@router.message(Command("admin"))
@router.message(MenuButton("menu.admin_panel"))
async def show_admin_panel(message: Message, user: User) -> None:
    await message.answer(
        "🛡️ <b>Admin Panel</b>\n\n"
        "<b>Quick Commands:</b>\n"
        "/adjust_balance — manually credit/debit a user's wallet\n"
        "/open_tickets — view the support queue and respond to tickets\n\n"
        "<b>Buttons:</b>\n"
        "📊 <b>Dashboard</b> — view analytics, sales stats, and user activity\n"
        "📦 <b>Products</b> — manage inventory and product listings\n"
        "📁 <b>Categories</b> — organize and edit product categories\n"
        "👥 <b>Users</b> — view and manage user accounts\n"
        "🛒 <b>Orders</b> — track and manage customer orders\n"
        "💰 <b>Payments</b> — monitor payment transactions and crypto deposits\n"
        "🎁 <b>Gift Codes</b> — create and manage gift code campaigns\n"
        "📢 <b>Broadcast</b> — send messages to all users\n"
        "⚙️ <b>Settings</b> — configure bot settings and preferences\n"
        "📝 <b>Logs</b> — view system logs and audit events",
        reply_markup=_panel_keyboard(user.locale),
    )


@router.callback_query(NavCB.filter(F.target == "admin_panel"), IsAdmin())
async def nav_admin_panel(query: CallbackQuery, callback_data: NavCB, user: User) -> None:  # noqa: ARG001
    if not query.message:
        return
    await query.message.edit_text(
        "🛡️ <b>Admin Panel</b>\n\n"
        "<b>Quick Commands:</b>\n"
        "/adjust_balance — manually credit/debit a user's wallet\n"
        "/open_tickets — view the support queue and respond to tickets\n\n"
        "<b>Buttons:</b>\n"
        "📊 <b>Dashboard</b> — view analytics, sales stats, and user activity\n"
        "📦 <b>Products</b> — manage inventory and product listings\n"
        "📁 <b>Categories</b> — organize and edit product categories\n"
        "👥 <b>Users</b> — view and manage user accounts\n"
        "🛒 <b>Orders</b> — track and manage customer orders\n"
        "💰 <b>Payments</b> — monitor payment transactions and crypto deposits\n"
        "🎁 <b>Gift Codes</b> — create and manage gift code campaigns\n"
        "📢 <b>Broadcast</b> — send messages to all users\n"
        "⚙️ <b>Settings</b> — configure bot settings and preferences\n"
        "📝 <b>Logs</b> — view system logs and audit events",
        reply_markup=_panel_keyboard(user.locale),
    )
    await query.answer()
