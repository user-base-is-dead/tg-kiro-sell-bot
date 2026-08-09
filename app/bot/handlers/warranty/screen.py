from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.callbacks import NavCB
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.common import back_keyboard, with_nav
from app.bot.keyboards.styles import NEUTRAL, PRIMARY, btn
from app.database.models.order import WarrantyStatus
from app.database.models.user import User
from app.database.repositories.warranty_repo import WarrantyRepo
from app.utils.pagination import Page

router = Router(name="warranty.screen")

_STATUS_EMOJI = {"ACTIVE": "🟢", "EXPIRED": "🔴", "CLAIMED": "🔵", "VOID": "⚫"}
PAGE_SIZE = 12


def _get_remaining_time(warranty) -> str:
    """Calculate remaining warranty time."""
    if warranty.status != WarrantyStatus.ACTIVE:
        return "N/A"

    now = datetime.now(UTC)
    if now > warranty.expires_at:
        return "Expired"

    remaining = warranty.expires_at - now
    hours = int(remaining.total_seconds() // 3600)
    days = hours // 24

    if days > 0:
        return f"{days}d {hours % 24}h"
    return f"{hours}h"


async def _render_warranty(repo, user: User, page_num: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    total = await repo.count_for_user(user.id)
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=total)
    warranties = await repo.list_for_user(user.id, limit=PAGE_SIZE, offset=page.offset)

    if not warranties:
        text = "🔧 <b>WARRANTY</b>\n\nYou have no active warranties yet."
        return text, back_keyboard(user.locale)

    blocks = []
    for w in warranties:
        item = await repo.get_order_item(w.order_item_id)
        product_name = item.product_name if item else "—"
        emoji = _STATUS_EMOJI.get(w.status.value, "•")
        remaining = _get_remaining_time(w)

        blocks.append(
            f"🛡️ <b>WARRANTY</b>\n\n"
            f"Product: {product_name}\n"
            f"📅 Started: {w.starts_at:%d %b %Y}\n"
            f"⏳ Expires: {w.expires_at:%d %b %Y}\n"
            f"⏱️ Remaining: {remaining}\n\n"
            f"{emoji} {w.status.value}"
        )

    text = "\n\n━━━━━━━━━━━━━━━━━━\n\n".join(blocks)

    # Add pagination info
    if page.total_pages > 1:
        text = f"🔧 <b>WARRANTY ({page.clamped_page}/{page.total_pages})</b>\n\n" + text

    # Build pagination keyboard
    nav_rows = []
    if page.has_prev:
        nav_rows.append(btn("◀️", f"warranty_page:{page.clamped_page - 1}", PRIMARY))
    if page.total_pages > 1:
        nav_rows.append(btn(f"{page.clamped_page}/{page.total_pages}", "noop", NEUTRAL))
    if page.has_next:
        nav_rows.append(btn("▶️", f"warranty_page:{page.clamped_page + 1}", PRIMARY))

    rows = []
    if nav_rows:
        rows.append(nav_rows)

    if warranties and warranties[0].status == WarrantyStatus.ACTIVE:
        rows.append([btn("🛡️ Claim Warranty", f"claim_warranty:{warranties[0].id}", PRIMARY)])

    rows.append([btn("🏠 Home", NavCB(target="home").pack(), PRIMARY)])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)

    return text, markup


@router.message(Command("warranty"))
@router.message(MenuButton("menu.warranty"))
async def cmd_warranty(message: Message, session, user: User) -> None:
    repo = WarrantyRepo(session)
    text, markup = await _render_warranty(repo, user)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("warranty_page:"))
async def warranty_page(query: CallbackQuery, session, user: User) -> None:
    if not query.message:
        return
    page_num = int(query.data.split(":")[1])
    repo = WarrantyRepo(session)
    text, markup = await _render_warranty(repo, user, page_num)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(NavCB.filter(F.target == "warranty"))
async def nav_warranty(query: CallbackQuery, callback_data: NavCB, session, user: User) -> None:  # noqa: ARG001
    if not query.message:
        return
    repo = WarrantyRepo(session)
    text, markup = await _render_warranty(repo, user)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()
