from __future__ import annotations

from aiogram.types import InlineKeyboardButton

from app.bot.callbacks import CategoryCB, ProductCB
from app.bot.keyboards.common import with_nav
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.database.models.catalog import Category, Product, ProductStatus
from app.locales.i18n import t
from app.services.catalog_service import ProductView
from app.utils.money import format_minor
from app.utils.pagination import Page
from app.utils.status_emoji import STATUS_EMOJI

# Button background per stock status, so a catalog page shows availability at a glance without the
# shopper reading a single label. Telegram offers no amber, so LOW_STOCK falls back to blue and
# leans on its 🟡 prefix; DISABLED stays unstyled because it is not meant to draw the eye.
_STATUS_STYLE: dict[ProductStatus, str | None] = {
    ProductStatus.IN_STOCK: SUCCESS,
    ProductStatus.LOW_STOCK: PRIMARY,
    ProductStatus.OUT_OF_STOCK: DANGER,
    ProductStatus.COMING_SOON: PRIMARY,
    ProductStatus.DISABLED: NEUTRAL,
}


def category_grid(categories: list[Category], locale: str):
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    # Alternating blue/green down the grid — the categories carry no state that would justify one
    # color over another, so the split is purely so adjacent tiles stay distinguishable.
    for i, cat in enumerate(categories):
        label = f"{cat.emoji or '📦'} {cat.name}"
        style = SUCCESS if i % 2 == 0 else PRIMARY
        row.append(btn(label, CategoryCB(action="open", id=str(cat.id)).pack(), style))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    # Top level of the store: Back is the way out to the main menu, so a separate Home would be the
    # same button twice.
    return with_nav(rows, locale, back_target="home", home=False)


def product_list(views: list[ProductView], category_id: int, page: Page, locale: str):
    rows: list[list[InlineKeyboardButton]] = []
    for view in views:
        p = view.product
        emoji = STATUS_EMOJI[view.display_status]
        label = f"{emoji} {p.name} — {format_minor(p.price_minor, p.currency)}"
        rows.append(
            [btn(label, ProductCB(action="view", id=str(p.id)).pack(), _STATUS_STYLE[view.display_status])]
        )

    if page.total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page.has_prev:
            nav.append(
                btn("◀️", CategoryCB(action="page", id=str(category_id), page=page.clamped_page - 1).pack(), PRIMARY)
            )
        # The indicator is a label, not an action — leaving it unstyled keeps the arrows tappable-looking.
        nav.append(
            btn(
                t("common.page_indicator", locale, page=page.clamped_page, total=page.total_pages),
                "noop",
                NEUTRAL,
            )
        )
        if page.has_next:
            nav.append(
                btn("▶️", CategoryCB(action="page", id=str(category_id), page=page.clamped_page + 1).pack(), PRIMARY)
            )
        rows.append(nav)

    return with_nav(rows, locale, back_target="categories")


def product_detail(product: Product, view: ProductView, locale: str, category_id: int):
    rows: list[list[InlineKeyboardButton]] = []
    if view.display_status.value in ("IN_STOCK", "LOW_STOCK"):
        rows.append([btn("🛒 Buy Now", ProductCB(action="buy", id=str(product.id)).pack(), SUCCESS)])
    return with_nav(rows, locale, back_target=f"cat-{category_id}")
