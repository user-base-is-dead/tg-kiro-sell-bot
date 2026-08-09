from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import OrderCB
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.orders import order_history_list
from app.database.models.user import User
from app.database.repositories.order_repo import OrderRepo
from app.locales.i18n import t
from app.utils.money import format_minor
from app.utils.pagination import Page

router = Router(name="orders.history")

PAGE_SIZE = 12


async def render_history(session: AsyncSession, user_id: int, page_num: int, locale: str) -> tuple[str, object]:
    repo = OrderRepo(session)
    total = await repo.count_for_user(user_id)
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=total)
    orders = await repo.list_for_user(user_id, offset=page.offset, limit=PAGE_SIZE)

    if not orders:
        return t("orders.empty_history", locale), order_history_list([], page, locale)

    return t("orders.history_title", locale), order_history_list(orders, page, locale)


async def _render_detail(session: AsyncSession, order_id: str, locale: str) -> tuple[str, object] | None:
    from app.bot.keyboards.common import with_nav

    order = await OrderRepo(session).get_by_id(order_id)
    if order is None:
        return None

    lines = [f"📦 <b>{order.order_number}</b>", f"Status: {order.status.value}", ""]
    for item in order.items:
        lines.append(f"• {item.product_name} — {format_minor(item.unit_price_minor, order.currency)} x{item.qty}")
    lines.append("")
    lines.append(f"Total: {format_minor(order.total_minor, order.currency)}")

    return "\n".join(lines), with_nav([], locale, back_target="orders", home=True)


@router.message(Command("orders"))
@router.message(MenuButton("menu.orders"))
async def cmd_orders(message: Message, session: AsyncSession, user: User) -> None:
    text, markup = await render_history(session, user.id, 1, user.locale)
    await message.answer(text, reply_markup=markup)


@router.callback_query(OrderCB.filter(F.action == "view"))
async def on_order_nav(query: CallbackQuery, callback_data: OrderCB, session: AsyncSession, user: User) -> None:
    if not query.message:
        return

    if callback_data.order_id:
        rendered = await _render_detail(session, callback_data.order_id, user.locale)
    else:
        rendered = await render_history(session, user.id, callback_data.page, user.locale)

    if rendered is None:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()
