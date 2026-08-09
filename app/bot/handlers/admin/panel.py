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
        "Use /adjust_balance to manually credit/debit a user's wallet.\n"
        "Use /open_tickets to see the support queue — reply inside a ticket's topic to chat with the user.",
        reply_markup=_panel_keyboard(user.locale),
    )


@router.callback_query(NavCB.filter(F.target == "admin_panel"), IsAdmin())
async def nav_admin_panel(query: CallbackQuery, callback_data: NavCB, user: User) -> None:  # noqa: ARG001
    if not query.message:
        return
    await show_admin_panel(query.message, user)
    await query.answer()
