from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminMiscCB
from app.bot.filters.is_admin import IsAdmin
from app.core.config import get_settings
from app.services.stats_service import get_dashboard_stats
from app.utils.money import format_minor

router = Router(name="admin.dashboard")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


async def _render(session: AsyncSession) -> str:
    s = await get_dashboard_stats(session)
    currency = get_settings().default_currency
    return (
        "📊 <b>DASHBOARD</b>\n\n"
        f"👥 Total Users: {s.total_users}\n"
        f"📦 Total Products: {s.total_products}\n"
        f"📁 Categories: {s.total_categories}\n"
        f"🛒 Total Orders: {s.total_orders}\n"
        f"⏳ Pending Orders: {s.pending_orders}\n"
        f"✅ Completed Orders: {s.completed_orders}\n"
        f"❌ Cancelled Orders: {s.cancelled_orders}\n"
        f"💰 Total Revenue: {format_minor(s.total_revenue_minor, currency)}\n"
        f"🟢 Products In Stock: {s.in_stock_products}\n"
        f"🔴 Products Out of Stock: {s.out_of_stock_products}\n\n"
        f"📅 Today: {s.orders_today} orders, {format_minor(s.revenue_today_minor, currency)}, {s.new_users_today} new users\n"
        f"📆 This week: {format_minor(s.revenue_week_minor, currency)}\n"
        f"🗓️ This month: {format_minor(s.revenue_month_minor, currency)}"
    )


@router.message(Command("dashboard"))
async def show_dashboard(message, session: AsyncSession) -> None:
    await message.answer(await _render(session))


@router.callback_query(AdminMiscCB.filter(F.action == "dashboard"))
async def show_dashboard_cb(query: CallbackQuery, session: AsyncSession) -> None:
    await query.message.edit_text(await _render(session))
    await query.answer()
