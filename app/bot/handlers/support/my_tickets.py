from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import SupportCB
from app.bot.keyboards.common import nav_row, with_nav
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.database.models.user import User
from app.database.repositories.support_repo import SupportRepo
from app.locales.i18n import t
from app.utils.pagination import Page

router = Router(name="support.my_tickets")

_STATUS_EMOJI = {"OPEN": "🟢", "PENDING": "🟡", "RESOLVED": "🔵", "CLOSED": "⚫"}
# Closed tickets go unstyled so the live ones are the only rows carrying color.
_STATUS_STYLE: dict[str, str | None] = {
    "OPEN": SUCCESS,
    "PENDING": PRIMARY,
    "RESOLVED": PRIMARY,
    "CLOSED": NEUTRAL,
}
PAGE_SIZE = 12


async def _render_tickets(session: AsyncSession, user: User, page_num: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    repo = SupportRepo(session)
    total = await repo.count_for_user(user.id)
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=total)
    tickets = await repo.list_for_user(user.id, limit=PAGE_SIZE, offset=page.offset)

    if not tickets:
        return t("support.no_tickets", user.locale), with_nav([], user.locale, back_target="support")

    rows = [
        [
            btn(
                f"{_STATUS_EMOJI.get(tk.status.value, '•')} {tk.ticket_number} — {tk.subject[:30]}",
                SupportCB(action="view", id=str(tk.id)).pack(),
                _STATUS_STYLE.get(tk.status.value, PRIMARY),
            )
        ]
        for tk in tickets
    ]

    # Add pagination
    nav_rows = []
    if page.has_prev:
        nav_rows.append(btn("◀️", f"tickets_page:{page.clamped_page - 1}", PRIMARY))
    if page.total_pages > 1:
        nav_rows.append(btn(f"{page.clamped_page}/{page.total_pages}", "noop", NEUTRAL))
    if page.has_next:
        nav_rows.append(btn("▶️", f"tickets_page:{page.clamped_page + 1}", PRIMARY))

    if nav_rows:
        rows.append(nav_rows)

    title = "📋 <b>MY TICKETS</b>"
    if page.total_pages > 1:
        title = f"📋 <b>MY TICKETS ({page.clamped_page}/{page.total_pages})</b>"

    return title, with_nav(rows, user.locale, back_target="support")


@router.callback_query(SupportCB.filter(F.action == "mytickets"))
async def list_my_tickets(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    text, markup = await _render_tickets(session, user)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(F.data.startswith("tickets_page:"))
async def tickets_page(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    if not query.message:
        return
    page_num = int(query.data.split(":")[1])
    text, markup = await _render_tickets(session, user, page_num)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(SupportCB.filter(F.action == "view"))
async def view_ticket(query: CallbackQuery, callback_data: SupportCB, session: AsyncSession, user: User) -> None:
    repo = SupportRepo(session)
    ticket = await repo.get_by_id(int(callback_data.id))
    if ticket is None or ticket.user_id != user.id:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return

    messages = await repo.list_messages(ticket.id, limit=10)
    lines = [f"🎫 <b>{ticket.ticket_number}</b> — {ticket.status.value}\n"]
    for m in messages[-8:]:
        author = "You" if m.author_type == "USER" else "Support"
        # Escaped: a message containing "<" would otherwise make Telegram reject the whole card,
        # and the user would just see their ticket stop opening.
        lines.append(f"<b>{author}:</b> {escape(m.content)}")
    if ticket.status.value in ("OPEN", "PENDING"):
        lines.append("\n<i>Reply here to continue the conversation.</i>")

    await query.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn(t("menu.back", user.locale), SupportCB(action="mytickets").pack(), DANGER)],
                nav_row(user.locale, home=True),
            ]
        ),
    )
    await query.answer()
