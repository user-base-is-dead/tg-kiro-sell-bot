from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.callbacks import NavCB
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.common import back_keyboard
from app.bot.keyboards.styles import NEUTRAL, PRIMARY, btn
from app.database.models.user import User
from app.database.repositories.warranty_repo import WarrantyRepo
from app.services.warranty_service import display_remaining, effective_status
from app.utils.pagination import Page

router = Router(name="warranty.screen")

_STATUS_EMOJI = {"ACTIVE": "🟢", "EXPIRED": "🔴", "CLAIMED": "🔵", "VOID": "⚫"}
_STATUS_LABEL = {"ACTIVE": "ACTIVE", "EXPIRED": "EXPIRED", "CLAIMED": "UNDER REVIEW", "VOID": "VOID"}
PAGE_SIZE = 12

EMPTY_TEXT = (
    "🛡️ <b>WARRANTY</b>\n\n"
    "You don't have any warranties yet.\n\n"
    "Every eligible product you buy comes with a warranty that starts automatically "
    "at the moment of purchase — nothing to register.\n\n"
    "Here you'll be able to:\n"
    "• See each warranty's start and expiry date\n"
    "• Check how much time is left\n"
    "• File a claim if something stops working\n\n"
    "🛒 Make your first purchase and it will show up right here."
)


def _truncate(name: str, limit: int = 22) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


async def _render_warranty(repo: WarrantyRepo, user: User, page_num: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    total = await repo.count_for_user(user.id)
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=total)
    warranties = await repo.list_for_user(user.id, limit=PAGE_SIZE, offset=page.offset)

    if not warranties:
        return EMPTY_TEXT, back_keyboard(user.locale)

    header = "🛡️ <b>WARRANTY</b>"
    if page.total_pages > 1:
        header += f"  <i>({page.clamped_page}/{page.total_pages})</i>"

    blocks = []
    rows = []
    for offset, w in enumerate(warranties):
        # Numbered against the full list, not the page, so item 13 is item 13 on page 2 and the
        # number under the text matches the button that claims it.
        number = page.offset + offset + 1
        product_name = w.order_item.product_name if w.order_item else "—"
        # Derived, not read off the row: the expiry sweep runs hourly, so a warranty that lapsed
        # ten minutes ago still stores ACTIVE and would otherwise render "expired 🟢 ACTIVE".
        status = effective_status(w)
        emoji = _STATUS_EMOJI.get(status.value, "•")
        label = _STATUS_LABEL.get(status.value, status.value)

        blocks.append(
            f"<b>{number}. {product_name}</b>\n"
            f"📅 {w.starts_at:%d %b %Y} → ⏳ {w.expires_at:%d %b %Y}\n"
            f"⏱️ {display_remaining(w)}   {emoji} {label}"
        )
        # A claim button on every item, including expired and already-claimed ones. Hiding it
        # leaves the customer with no way to ask why, so the button always exists and the handler
        # explains the situation.
        rows.append([btn(f"🛡️ Claim #{number} · {_truncate(product_name)}", f"wclaim:{w.id}", PRIMARY)])

    text = header + "\n\n" + "\n\n━━━━━━━━━━━━━━━━━━\n\n".join(blocks)

    nav_rows = []
    if page.has_prev:
        nav_rows.append(btn("◀️ Prev", f"warranty_page:{page.clamped_page - 1}", PRIMARY))
    if page.total_pages > 1:
        nav_rows.append(btn(f"{page.clamped_page}/{page.total_pages}", "noop", NEUTRAL))
    if page.has_next:
        nav_rows.append(btn("Next ▶️", f"warranty_page:{page.clamped_page + 1}", PRIMARY))
    if nav_rows:
        rows.append(nav_rows)

    rows.append([btn("🏠 Home", NavCB(target="home").pack(), PRIMARY)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("warranty"))
@router.message(MenuButton("menu.warranty"))
async def cmd_warranty(message: Message, session, user: User) -> None:
    text, markup = await _render_warranty(WarrantyRepo(session), user)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("warranty_page:"))
async def warranty_page(query: CallbackQuery, session, user: User) -> None:
    if not query.message:
        return
    page_num = int(query.data.split(":")[1])
    text, markup = await _render_warranty(WarrantyRepo(session), user, page_num)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(NavCB.filter(F.target == "warranty"))
async def nav_warranty(query: CallbackQuery, callback_data: NavCB, session, user: User) -> None:  # noqa: ARG001
    if not query.message:
        return
    text, markup = await _render_warranty(WarrantyRepo(session), user)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()
