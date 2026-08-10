from __future__ import annotations

from aiogram.types import InlineKeyboardButton

from app.bot.callbacks import OrderCB
from app.bot.keyboards.common import with_nav
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.database.models.order import Order
from app.utils.money import format_minor
from app.utils.pagination import Page

# Public because the order *detail* screen heads its card with the same badge the list row carries —
# one map, so a status can never wear two different colors on two screens.
STATUS_EMOJI = {
    "PENDING": "🟡",
    "PROCESSING": "🔵",
    "COMPLETED": "🟢",
    "CANCELLED": "🔴",
    "FAILED": "⚠️",
}

# Order rows are colored by outcome: delivered is green, in-flight is blue, dead orders are red.
_STATUS_STYLE: dict[str, str | None] = {
    "PENDING": PRIMARY,
    "PROCESSING": PRIMARY,
    "COMPLETED": SUCCESS,
    "CANCELLED": DANGER,
    "FAILED": DANGER,
}


def order_history_list(orders: list[Order], page: Page, locale: str):
    rows: list[list[InlineKeyboardButton]] = []
    for o in orders:
        emoji = STATUS_EMOJI.get(o.status.value, "•")
        label = f"{emoji} {o.order_number} — {format_minor(o.total_minor, o.currency)}"
        style = _STATUS_STYLE.get(o.status.value, PRIMARY)
        rows.append([btn(label, OrderCB(action="view", order_id=o.id).pack(), style)])

    if page.total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page.has_prev:
            nav.append(btn("◀️", OrderCB(action="view", page=page.clamped_page - 1).pack(), PRIMARY))
        nav.append(btn(f"{page.clamped_page}/{page.total_pages}", "noop", NEUTRAL))
        if page.has_next:
            nav.append(btn("▶️", OrderCB(action="view", page=page.clamped_page + 1).pack(), PRIMARY))
        rows.append(nav)

    return with_nav(rows, locale, back_target="home", home=False)
