from __future__ import annotations

from aiogram.types import InlineKeyboardButton

from app.bot.callbacks import CategoryCB, ProductCB
from app.bot.keyboards.common import with_nav
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.database.models.catalog import Category, Product, ProductStatus
from app.locales.i18n import t
from app.services.catalog_service import ProductView, stock_label
from app.utils.money import format_minor
from app.utils.pagination import Page
from app.utils.status_emoji import STATUS_EMOJI

# Button background per stock status, so a catalog page shows availability at a glance without the
# shopper reading a single label. Telegram offers no amber, so LOW_STOCK falls back to blue and
# leans on its 🟡 prefix; DISABLED stays unstyled because it is not meant to draw the eye.
_STATUS_STYLE: dict[ProductStatus, str | None] = {
    ProductStatus.IN_STOCK: SUCCESS,
    ProductStatus.LOW_STOCK: SUCCESS,
    ProductStatus.ON_HOLD: DANGER,
    ProductStatus.OUT_OF_STOCK: DANGER,
    ProductStatus.COMING_SOON: PRIMARY,
    ProductStatus.DISABLED: NEUTRAL,
}


def category_grid(categories: list[Category], locale: str, *, loose: list["ProductView"] | None = None):
    rows: list[list[InlineKeyboardButton]] = []

    for view in loose or []:
        p = view.product
        emoji = STATUS_EMOJI[view.display_status]
        style = _STATUS_STYLE[view.display_status]
        rows.append(
            [
                btn(
                    f"{emoji} {p.name} — {format_minor(p.price_minor, p.currency)}",
                    ProductCB(action="view", id=str(p.id)).pack(),
                    style,
                )
            ]
        )
    if loose:
        rows.append([btn("─────────────", "noop", NEUTRAL)])

    row: list[InlineKeyboardButton] = []
    # Every category is blue. Green is reserved for state that means "available"/"go ahead" further
    # in (a stocked product, Buy Now), so spending it on folders — which carry no state at all —
    # made the two levels look like they were saying the same thing.
    for cat in categories:
        label = f"{cat.emoji or '📦'} {cat.name}"
        row.append(btn(label, CategoryCB(action="open", id=str(cat.id)).pack(), PRIMARY))
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
        # The count rides on the button itself so a shopper sees scarcity before tapping in.
        left = stock_label(view)
        if left:
            label += f" · {left}"
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


def category_back_target(category_id: int | None) -> str:
    """Back token for a screen showing one product.

    nav.py resolves this with `int(target.removeprefix("cat-"))`, so a product filed outside every
    category cannot use the "cat-" form at all — `cat-None` raises ValueError there and the user
    gets the generic "something went wrong" alert instead of a screen. Loose products go back to
    the store root, which is where they are listed anyway.
    """
    return f"cat-{category_id}" if category_id is not None else "categories"


def product_detail(product: Product, view: ProductView, locale: str, category_id: int | None):
    rows: list[list[InlineKeyboardButton]] = []
    if view.display_status.value in ("IN_STOCK", "LOW_STOCK"):
        rows.append([btn("🛒 Buy Now", ProductCB(action="buy", id=str(product.id)).pack(), SUCCESS)])
    # "buy" opens the payment-method chooser, not the confirm screen — see
    # orders/checkout.py:render_payment_choice.
    return with_nav(rows, locale, back_target=category_back_target(category_id))
