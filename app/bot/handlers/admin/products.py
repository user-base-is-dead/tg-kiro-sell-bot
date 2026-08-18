from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminProductCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.common import nav_row
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.bot.states.product_form import (
    ProductEditForm,
    ProductForm,
    ProductImportForm,
    ProductSearchForm,
    StockUploadForm,
)
from app.core.config import get_settings
from app.database.models.catalog import (
    Category,
    FulfillmentMode,
    Product,
    ProductStatus,
    StockItem,
)
from app.database.repositories.category_repo import CategoryRepo
from app.database.repositories.product_repo import ProductRepo
from app.services.announcement_service import announce_new_product, announce_restock
from app.services.catalog_service import (
    add_stock,
    compute_display_status,
    create_product,
    resync_status as _resync_status,
)
from app.services.product_import import MAX_BYTES, MAX_ROWS, apply_rows, parse_csv, to_csv
from app.utils.money import format_minor, parse_to_minor
from app.utils.pagination import Page
from app.utils.status_emoji import STATUS_EMOJI
from app.utils.text import PAD, as_admin_wrote_it

logger = logging.getLogger(__name__)

router = Router(name="admin.products")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

PAGE_SIZE = 10

# The wizard's step order. Back walks one entry left; Abort leaves entirely. Keeping the order in
# one tuple means a new step cannot be added without also being reachable by Back.
_PRODUCT_STEPS: tuple[str, ...] = (
    "category",
    "name",
    "description",
    "price",
    "fulfillment_mode",
    "warranty_days",
    "delivery_info",
    "stock",
    "stock_count",
)

_STEP_STATES = {
    "category": ProductForm.category,
    "name": ProductForm.name,
    "description": ProductForm.description,
    "price": ProductForm.price,
    "fulfillment_mode": ProductForm.fulfillment_mode,
    "warranty_days": ProductForm.warranty_days,
    "delivery_info": ProductForm.delivery_info,
    "stock": ProductForm.stock,
    "stock_count": ProductForm.stock_count,
}

_TOTAL_STEPS = len(_PRODUCT_STEPS)

# Short codes, not field names: callback_data is capped at 64 bytes and has to carry the id too.
_EDIT_FIELDS: dict[str, str] = {
    "nm": "Name",
    "pr": "Price",
    "ds": "Description",
    "wr": "Warranty",
    "md": "Fulfillment mode",
    "ct": "Category",
    "dv": "Delivery info",
    "st": "Stock count",
}

# Said once, shown in both the wizard and the edit screen — the rule is subtle enough that it has
# to be spelled out where the number is actually typed, and two different explanations of the same
# rule is how the two screens start disagreeing.
_STOCK_COUNT_HELP = (
    "How many are on sale?\n\n"
    "Send a number to set it by hand, or leave it automatic and the store will simply count the "
    "credentials you loaded.\n\n"
    "On <b>⚡ Auto</b> the number may go <b>above</b> the credential count: buyers keep getting a "
    "credential auto-delivered while any are left, and only the units beyond that land in your "
    "<b>pending fulfilment</b> queue. The product stays on Auto — the mode button shows the split.\n\n"
    "On <b>🙋 Manual</b> the number is the whole story: your credentials are frozen and never "
    "touched, whatever you set here.\n\n"
    "<code>0</code> closes the product to buyers without disabling it, and without spending a "
    "single credential."
)


def _parse_stock_count(raw: str) -> int | None:
    """A whole count of 0 or more, or None if that isn't what was typed.

    Deliberately strict: "10 keys" or "-3" are rejected rather than coerced, because every one of
    those guesses would silently put a wrong number in front of buyers.
    """
    text = raw.strip()
    return int(text) if text.isdigit() else None


def _manual_stock_tail(manual_stock: int, credentials: int) -> str:
    """What a hand-set count actually means for this product, in the admin's terms."""
    if manual_stock == 0:
        return "🔴 Stock set to <b>0</b> — shows OUT OF STOCK and takes no orders."
    beyond = manual_stock - credentials
    if beyond > 0:
        return (
            f"✅ <b>LIVE</b> with <b>{manual_stock}</b> on sale.\n"
            f"⚡ First {credentials} auto-delivered from credentials, "
            f"the other {beyond} land in your pending fulfilment queue."
            if credentials
            else f"✅ <b>LIVE</b> with <b>{manual_stock}</b> on sale — all hand-fulfilled, "
            "no credentials loaded."
        )
    return f"✅ <b>LIVE</b> with <b>{manual_stock}</b> on sale, all auto-delivered."


def _step_keyboard(
    step: str, *, extra: list[list[InlineKeyboardButton]] | None = None
) -> InlineKeyboardMarkup:
    """Every wizard screen ends with [🔙 Back] [❌ Abort]. Red on both: they are the two ways to
    leave, and nav_row already established that leaving is red everywhere else in the bot."""
    rows = list(extra or [])
    rows.append(
        [
            btn("🔙 Back", f"pback:{step}", DANGER),
            btn("❌ Abort", "pabort", DANGER),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Telegram's own ceiling for a button label. Names here are admin-written and run long — "Chatgpt
# plus 1months (apple pay) 7days Warranty" — so the label is composed tail-first and the name is
# what gives way, never the price or the stock count.
_MAX_LABEL = 64


async def _product_button(session: AsyncSession, product) -> InlineKeyboardButton:
    """One product, with the two numbers the admin actually came to check.

    The list used to show only a dot and a price, which made two products called "Kiro Pro" at
    different prices impossible to tell apart, and said nothing about stock — the one thing that
    decides whether a listed product is really for sale. Finding out meant opening each in turn.
    """
    view = await compute_display_status(session, product)
    if product.manual_stock is None and product.fulfillment_mode is FulfillmentMode.MANUAL:
        stock = "on demand"
    else:
        stock = f"{view.available_stock} left"

    dot = "🟢" if product.is_active else "⚫"
    tail = f" — {format_minor(product.price_minor, product.currency)} · {stock}"
    room = _MAX_LABEL - len(dot) - 1 - len(tail)
    name = product.name if len(product.name) <= room else product.name[: max(1, room - 1)] + "…"
    return btn(
        f"{dot} {name}{tail}",
        AdminProductCB(action="view", id=str(product.id)).pack(),
        PRIMARY if product.is_active else NEUTRAL,
    )


def _page_nav(page: Page, action: str, category_id: str = "") -> list[InlineKeyboardButton]:
    nav: list[InlineKeyboardButton] = []
    if page.total_pages <= 1:
        return nav
    # The arrows are laid out as a fixed three-slot row rather than appearing and disappearing:
    # with only "1/2 ▶️" on the first page the indicator sat off-centre and the row jumped sideways
    # on every page turn.
    nav.append(
        btn("◀️", AdminProductCB(action=action, id=category_id, page=page.clamped_page - 1).pack(), PRIMARY)
        if page.has_prev
        else btn(" ", "noop", NEUTRAL)
    )
    nav.append(btn(f"{page.clamped_page}/{page.total_pages}", "noop", NEUTRAL))
    nav.append(
        btn("▶️", AdminProductCB(action=action, id=category_id, page=page.clamped_page + 1).pack(), PRIMARY)
        if page.has_next
        else btn(" ", "noop", NEUTRAL)
    )
    return nav


def _tools_rows(name_like: str | None = None) -> list[list[InlineKeyboardButton]]:
    """The management tools, identical on every products screen so they never move under the thumb."""
    search_label = f"🔍 Filtered: {name_like}" if name_like else "🔍 Search"
    return [
        [btn("➕ Add Product", AdminProductCB(action="add").pack(), SUCCESS)],
        [
            btn("📥 Import CSV", AdminProductCB(action="import").pack(), PRIMARY),
            btn("📤 Export CSV", AdminProductCB(action="export").pack(), PRIMARY),
        ],
        [btn(search_label, AdminProductCB(action="search").pack(), PRIMARY)],
        nav_row("en", back_target="admin_panel", home=False),
    ]

def _fulfillment_label(product, credentials: int) -> str:
    """What will actually happen to the next orders, not just which mode is stored.

    A hand-set count above the credential pool does not switch the product to MANUAL — the pool is
    still auto-delivered, and only the units past it are hand-fulfilled. The button said plain
    "Auto" through all of that, which reads as "nothing here needs your attention" about a product
    that is about to start filling the pending queue.
    """
    if product.fulfillment_mode is not FulfillmentMode.AUTO:
        return "🙋 Fulfillment: Manual"
    by_hand = (product.manual_stock or 0) - credentials
    if product.manual_stock is not None and by_hand > 0:
        return f"🔀 Auto ×{credentials} + Manual ×{by_hand}"
    return "⚡ Fulfillment: Auto"


def _detail_keyboard(product, credentials: int = 0) -> InlineKeyboardMarkup:
    """One flat screen: every field is one tap from the product, not two.

    There used to be an `✏️ Edit` button that opened a second screen asking "which field?" — a whole
    extra tap and message to answer a question the buttons can just ask directly. The fields live
    here now, paired two-per-row so the screen stays short.
    """
    pid = str(product.id)
    toggle_label = "🔴 Disable" if product.is_active else "🟢 Enable"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn("✏️ Name", f"pedit:nm:{pid}", PRIMARY),
                btn("💰 Price", f"pedit:pr:{pid}", PRIMARY),
            ],
            [
                btn("📝 Description", f"pedit:ds:{pid}", PRIMARY),
                btn("🏷️ Category", f"pedit:ct:{pid}", PRIMARY),
            ],
            [
                btn("🛡️ Warranty", f"pedit:wr:{pid}", PRIMARY),
                # The button carries the live state, not just the field name. Auto and Manual
                # behave completely differently at checkout, and "⚡ Fulfillment" gave no way to
                # tell which one a product was on without opening the editor to find out.
                btn(_fulfillment_label(product, credentials), f"pedit:md:{pid}", PRIMARY),
            ],
            [
                btn("🚚 Delivery info", f"pedit:dv:{pid}", PRIMARY),
                # Sits next to Add Stock deliberately: one loads credentials, the other decides how
                # many the store says there are, and they are routinely changed in the same sitting.
                btn(
                    f"🔢 Stock count: {product.manual_stock}"
                    if product.manual_stock is not None
                    else "🔢 Stock count: auto",
                    f"pedit:st:{pid}",
                    PRIMARY,
                ),
            ],
            [btn("📦 Add Stock", AdminProductCB(action="stock", id=pid).pack(), SUCCESS)],
            [
                btn(
                    toggle_label,
                    AdminProductCB(action="toggle", id=pid).pack(),
                    DANGER if product.is_active else SUCCESS,
                )
            ],
            [btn("🗑️ Delete", AdminProductCB(action="delete", id=pid).pack(), DANGER)],
            [btn("🔙 Back", AdminProductCB(action="list").pack(), DANGER)],
        ]
    )


# Both queries sit behind module-level seams so a screen-copy test can render the list without a
# database, and so the count is the only query that always runs.
async def _count_products(session: AsyncSession, *, name_like: str | None = None) -> int:
    return await ProductRepo(session).count_all(name_like=name_like)


async def _list_page(
    session: AsyncSession, *, offset: int, limit: int, name_like: str | None = None
) -> list:
    return await ProductRepo(session).list_page(offset=offset, limit=limit, name_like=name_like)


# The grouped list needs four more queries, and they go through seams for the same reason the first
# two did: a screen-copy test renders this list with no database behind it. `active_only=False`
# everywhere — an admin's list that hid disabled products would hide the ones needing attention.
async def _count_loose(session: AsyncSession) -> int:
    return await ProductRepo(session).count_uncategorized(active_only=False)


async def _list_loose(session: AsyncSession, *, offset: int, limit: int) -> list:
    return await ProductRepo(session).list_uncategorized(
        offset=offset, limit=limit, active_only=False
    )


async def _list_categories(session: AsyncSession) -> list:
    return await CategoryRepo(session).list_all()


async def _count_in_category(session: AsyncSession, category_id: int) -> int:
    return await ProductRepo(session).count_by_category(category_id, active_only=False)


async def _list_in_category(
    session: AsyncSession, category_id: int, *, offset: int, limit: int
) -> list:
    return await ProductRepo(session).list_by_category(
        category_id, offset=offset, limit=limit, active_only=False
    )


async def _render_list(
    session: AsyncSession, page_num: int, *, name_like: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    """The catalog as it is actually shaped: loose products out in the open, categories as folders.

    A flat list of everything is fine at ten products and unusable at eighty — one wall of buttons
    with no grouping, where the only way to find the Kiro products is to remember which page they
    were on. This mirrors the store and the broadcast picker, so the same catalog looks the same
    everywhere.

    A search is the exception and stays flat: the whole point of searching is to reach across
    categories, and folding results back into folders would hide the match that was asked for.
    """
    if name_like:
        return await _render_search(session, page_num, name_like)

    total = await _count_products(session)
    loose_total = await _count_loose(session)
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=loose_total)
    loose = (
        await _list_loose(session, offset=page.offset, limit=PAGE_SIZE) if loose_total else []
    )
    categories = await _list_categories(session)

    rows = [[await _product_button(session, p)] for p in loose]
    if nav := _page_nav(page, "list"):
        rows.append(nav)

    if categories:
        if loose:
            rows.append([btn("─────────────", "noop", NEUTRAL)])
        folders = []
        for category in categories:
            count = await _count_in_category(session, category.id)
            mark = "" if category.is_active else "⚫ "
            folders.append(
                btn(
                    f"{mark}{category.emoji or '📂'} {category.name} ({count})",
                    AdminProductCB(action="cat", id=str(category.id)).pack(),
                    PRIMARY,
                )
            )
        rows += [folders[i : i + 2] for i in range(0, len(folders), 2)]

    rows += _tools_rows()

    filed = total - loose_total
    text = (
        "📦 <b>PRODUCT MANAGEMENT</b>\n\n"
        f"{total} product(s) — {loose_total} loose, {filed} in {len(categories)} category folder(s).\n\n"
        "Tap a product to edit it, or open a folder to see what is inside.\n"
        "🟢 active · ⚫ hidden from buyers · <b>N left</b> = sellable stock right now\n\n"
        "➕ <b>Add Product</b> — create one, stock included\n"
        "📥 <b>Import CSV</b> / 📤 <b>Export CSV</b> — edit the whole catalog in one file\n"
        "🔍 <b>Search</b> — find a product by name, across every folder"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_search(
    session: AsyncSession, page_num: int, name_like: str
) -> tuple[str, InlineKeyboardMarkup]:
    total = await _count_products(session, name_like=name_like)
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=total)
    products = (
        await _list_page(session, offset=page.offset, limit=PAGE_SIZE, name_like=name_like)
        if total
        else []
    )

    rows = [[await _product_button(session, p)] for p in products]
    if nav := _page_nav(page, "list"):
        rows.append(nav)
    rows += _tools_rows(name_like)

    text = (
        "📦 <b>PRODUCT MANAGEMENT</b>\n\n"
        f"🔍 {total} match(es) for “{name_like}”, from every category.\n\n"
        "Tap the filter button below to clear it and go back to the folders."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_category(
    session: AsyncSession, category_id: int, page_num: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    category = await CategoryRepo(session).get_by_id(category_id)
    if category is None:
        return None

    total = await _count_in_category(session, category_id)
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=total)
    products = (
        await _list_in_category(session, category_id, offset=page.offset, limit=PAGE_SIZE)
        if total
        else []
    )

    rows = [[await _product_button(session, p)] for p in products]
    if nav := _page_nav(page, "cat", str(category_id)):
        rows.append(nav)
    # Not a 🔙, and not red: the tools row below already ends in a red Back to the admin panel, and
    # two red back-arrows one above the other are a coin toss over which screen you land on.
    rows.append([btn("📦 All products", AdminProductCB(action="list").pack(), PRIMARY)])
    rows += _tools_rows()

    # The same briefing the top-level list carries. A folder is not a lesser screen — every product
    # inside it is edited from here, and the sold-out count and the ⚫ marks mean what they mean on
    # both screens, so explaining them on only one of the two is where the confusion starts.
    emoji = category.emoji or "📂"
    active = sum(1 for p in products if p.is_active)
    sold_out = 0
    for product in products:
        view = await compute_display_status(session, product)
        if product.is_active and view.display_status is ProductStatus.OUT_OF_STOCK:
            sold_out += 1

    lines = [f"{emoji} <b>{category.name.upper()}</b>", ""]
    if not category.is_active:
        lines += [
            "⚫ <b>This whole folder is hidden from buyers.</b>",
            "Enable it under 📁 Categories — until then nothing inside it is for sale, however "
            "much stock it has.",
            "",
        ]
    if total:
        lines.append(f"{total} product(s) in here — {active} on sale, {total - active} hidden.")
        if sold_out:
            lines.append(
                f"🔴 {sold_out} of them {'is' if sold_out == 1 else 'are'} live with no stock left. "
                "Buyers can see those but cannot buy them — add stock or hide them."
            )
        lines += [
            "",
            "Tap a product to change its price, stock, description or category, or to hide or "
            "delete it.",
            "🟢 active · ⚫ hidden from buyers · <b>N left</b> = sellable stock right now",
        ]
    else:
        lines += [
            "Nothing filed here yet.",
            "",
            "Use ➕ <b>Add Product</b> and pick this category on the way through, or open an "
            "existing product and change its 🏷️ Category to move it in here.",
        ]
    lines += [
        "",
        "➕ <b>Add Product</b> — create one, stock included",
        "📥 <b>Import CSV</b> / 📤 <b>Export CSV</b> — edit the whole catalog in one file",
        "🔍 <b>Search</b> — find a product by name, across every folder",
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

async def _render_detail(session: AsyncSession, product_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    product = await ProductRepo(session).get_by_id(product_id)
    if product is None:
        return None

    view = await compute_display_status(session, product)
    category = "🚫 none" if product.category_id is None else "—"
    if product.category_id is not None:
        found = await CategoryRepo(session).get_by_id(product.category_id)
        category = found.name if found else "—"

    credentials = await ProductRepo(session).available_stock_count(product.id)
    if product.fulfillment_mode is FulfillmentMode.MANUAL:
        # Credentials are frozen in MANUAL mode — never claimed, never delivered — so they are
        # reported as parked rather than omitted. An admin who loaded 34 keys and then switched
        # modes needs to see that all 34 are still there.
        parked = f" · {credentials} credential(s) parked, untouched" if credentials else ""
        on_sale = f"{product.manual_stock} on sale (set by hand)" if product.manual_stock is not None else "unlimited"
        stock_line = f"{on_sale}{parked}"
    elif product.manual_stock is not None:
        # Both numbers, always: the difference between them is exactly the set of sales that will
        # arrive as manual fulfilment, and that is not something to make an admin work out.
        stock_line = f"{product.manual_stock} on sale (set by hand) · {credentials} credential(s) loaded"
    else:
        stock_line = str(view.available_stock)
    text = (
        f"{STATUS_EMOJI[view.display_status]} <b>{product.name}</b>\n\n"
        f"Price: {format_minor(product.price_minor, product.currency)}\n"
        f"Category: {category}\n"
        f"Stock: {stock_line}\n"
        # Same wording as the button below it — two descriptions of one thing is how a screen
        # starts contradicting itself.
        f"Fulfillment: {_fulfillment_label(product, credentials).split(': ')[-1]}\n"
        f"Warranty: {product.warranty_days} days\n"
        f"Description: {product.description or '—'}\n"
        f"Delivery info: {product.delivery_info or '—'}\n"
        f"{PAD}"
    )
    return text, _detail_keyboard(product, credentials)


@router.callback_query(AdminProductCB.filter(F.action == "list"))
async def list_products(
    query: CallbackQuery, callback_data: AdminProductCB, state: FSMContext, session: AsyncSession
) -> None:
    """The search term cannot ride in callback_data within 64 bytes, so it lives in FSM data and is
    read back here — that is what keeps paging inside a filtered set filtered."""
    name_like = (await state.get_data()).get("product_filter")
    text, markup = await _render_list(session, callback_data.page, name_like=name_like)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(AdminProductCB.filter(F.action == "cat"))
async def list_category(
    query: CallbackQuery, callback_data: AdminProductCB, state: FSMContext, session: AsyncSession
) -> None:
    """Inside one folder. Opening a folder drops any active search — the two are different questions
    and leaving the filter on made a folder look half-empty for no visible reason."""
    await state.update_data(product_filter=None)
    rendered = await _render_category(session, int(callback_data.id), callback_data.page)
    if rendered is None:
        await query.answer("Category not found.", show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(AdminProductCB.filter(F.action == "view"))
async def view_product(
    query: CallbackQuery, callback_data: AdminProductCB, session: AsyncSession, state: FSMContext
) -> None:
    # This is the `🔙 Back` out of Add Stock and every edit prompt, so it has to drop the form state.
    # Leaving it set is how the admin's next unrelated message silently got eaten as a stock item.
    await state.clear()
    rendered = await _render_detail(session, int(callback_data.id))
    if rendered is None:
        await query.answer("Product not found.", show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()

@router.callback_query(AdminProductCB.filter(F.action == "toggle"))
async def toggle_product(
    query: CallbackQuery, callback_data: AdminProductCB, session: AsyncSession, state: FSMContext
) -> None:
    product = await ProductRepo(session).get_by_id(int(callback_data.id))
    if product is None:
        await query.answer("Product not found.", show_alert=True)
        return
    product.is_active = not product.is_active
    await session.flush()
    await view_product(query, callback_data, session, state)


@router.callback_query(AdminProductCB.filter(F.action == "delete"))
async def confirm_delete(query: CallbackQuery, callback_data: AdminProductCB, session: AsyncSession) -> None:
    """Ask first. Delete is the one button on this screen that cannot be undone."""
    product = await ProductRepo(session).get_by_id(int(callback_data.id))
    if product is None:
        await query.answer("Product not found.", show_alert=True)
        return
    await query.message.edit_text(
        f"⚠️ Delete <b>{product.name}</b> permanently?\n\n"
        "<i>Past orders keep their name, price and delivered items, and buyers keep their warranty "
        "— only the catalog entry goes. Unsold stock for it is discarded, and any gift code that "
        "granted it is disabled.</i>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("🗑️ Yes, delete", AdminProductCB(action="delete_ok", id=callback_data.id).pack(), DANGER)],
                [btn("🔙 No, keep it", AdminProductCB(action="view", id=callback_data.id).pack(), PRIMARY)],
            ]
        ),
    )
    await query.answer()


@router.callback_query(AdminProductCB.filter(F.action == "delete_ok"))
async def delete_product(
    query: CallbackQuery, callback_data: AdminProductCB, state: FSMContext, session: AsyncSession
) -> None:
    """Delete for real. `ProductRepo.delete` detaches order history first, so past orders survive.

    This used to refuse outright with a "this product has order history, disable it instead" alert
    — a dead end that left the admin to go press Disable themselves. The FK that caused it is now
    nullable on both `order_items` and `stock_items` (migration 0013), so nothing blocks the delete.
    The disable path below is only a last-resort net for a reference nobody anticipated: it should
    never fire, and the error is logged loudly if it does.
    """
    product = await ProductRepo(session).get_by_id(int(callback_data.id))
    if product is None:
        await query.answer("Product not found.", show_alert=True)
        return

    name = product.name
    try:
        await ProductRepo(session).delete(product)
        await session.flush()
        note = f"🗑️ Deleted <b>{name}</b>."
        answer = "Deleted."
    except IntegrityError:
        logger.exception("Unexpected reference blocked deleting product %s", callback_data.id)
        await session.rollback()
        # rollback() expires the identity map, so re-fetch before touching the row again.
        product = await ProductRepo(session).get_by_id(int(callback_data.id))
        if product is not None:
            product.is_active = False
            await session.flush()
        note = (
            f"⚠️ <b>{name}</b> is still referenced somewhere, so it was <b>disabled</b> "
            "(hidden from buyers) instead of deleted. Nothing was lost."
        )
        answer = "Disabled instead."

    await query.answer(answer)
    name_like = (await state.get_data()).get("product_filter")
    text, markup = await _render_list(session, 1, name_like=name_like)
    await query.message.edit_text(f"{note}\n\n{text}", reply_markup=markup)


# ---- Add Product wizard ----


@router.callback_query(AdminProductCB.filter(F.action == "add"))
async def start_add(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await _show_step(query.message, state, "category", session)
    await query.answer()

async def _show_step(
    message: Message, state: FSMContext, step: str, session: AsyncSession, *, edit: bool = True
) -> None:
    """One renderer per step, shared by forward progress and by Back, so the same step can never
    draw two different screens. `edit` is False when the trigger was the admin's own typed reply —
    their message cannot be edited, so the next step is sent as a new one."""
    await state.set_state(_STEP_STATES[step])
    number = _PRODUCT_STEPS.index(step) + 1
    head = f"➕ <b>Add Product</b> — step {number} of {_TOTAL_STEPS}\n\n"
    send = message.edit_text if edit else message.answer

    if step == "category":
        categories = await CategoryRepo(session).list_active()
        rows = [[btn(f"{c.emoji or '📦'} {c.name}", f"pickcat:{c.id}", PRIMARY)] for c in categories]
        rows.append([btn("🚫 No category", "pickcat:none", PRIMARY)])
        await send(
            f"{head}Choose a category, or file it outside them — a product with no category is "
            "listed on its own above the folders in the store.",
            reply_markup=_step_keyboard("category", extra=rows),
        )
        return

    if step == "name":
        await send(
            f"{head}Send the product name (1-128 characters):",
            reply_markup=_step_keyboard("name"),
        )
        return

    if step == "description":
        await send(
            f"{head}Send a description buyers will see on the product page, or skip it:",
            reply_markup=_step_keyboard(
                "description", extra=[[btn("⏭️ Skip", "pskip:description", PRIMARY)]]
            ),
        )
        return

    if step == "price":
        await send(
            f"{head}Send the price, e.g. <code>9.99</code>.\n"
            "USD is assumed unless you write the currency: <code>9.99 EUR</code>",
            reply_markup=_step_keyboard("price"),
        )
        return

    if step == "fulfillment_mode":
        await send(
            f"{head}How does this product get delivered?\n\n"
            "⚡ <b>Auto</b> — a stock item (licence key, account) is sent the moment payment clears.\n"
            "🙋 <b>Manual</b> — the order lands in your queue and you fulfil it yourself.",
            reply_markup=_step_keyboard(
                "fulfillment_mode",
                extra=[[btn("⚡ Auto", "pmode:auto", PRIMARY), btn("🙋 Manual", "pmode:manual", PRIMARY)]],
            ),
        )
        return

    if step == "warranty_days":
        await send(
            f"{head}How long can a buyer file a warranty claim after purchase?",
            reply_markup=_step_keyboard(
                "warranty_days",
                extra=[
                    [
                        btn("None", "pwar:0", PRIMARY),
                        btn("7d", "pwar:7", PRIMARY),
                        btn("30d", "pwar:30", PRIMARY),
                    ],
                    [
                        btn("90d", "pwar:90", PRIMARY),
                        btn("365d", "pwar:365", PRIMARY),
                        btn("✏️ Custom", "pwar:custom", PRIMARY),
                    ],
                ],
            ),
        )
        return

    if step == "delivery_info":
        await send(
            f"{head}Instructions shown to the buyer after purchase — where to redeem, how to log in. "
            "Skip if the stock item speaks for itself:",
            reply_markup=_step_keyboard(
                "delivery_info", extra=[[btn("⏭️ Skip", "pskip:delivery_info", PRIMARY)]]
            ),
        )
        return

    if step == "stock":
        await send(
            f"{head}Send the stock item — a licence key, or account credentials on as many lines "
            "as you need. <b>One message = one item.</b> "
            "They are encrypted before they touch the database.\n\n"
            "Skip to create the product OUT OF STOCK and add them later.",
            reply_markup=_step_keyboard("stock", extra=[[btn("⏭️ Skip", "pskip:stock", PRIMARY)]]),
        )
        return

    if step == "stock_count":
        await send(
            f"{head}{_STOCK_COUNT_HELP}",
            reply_markup=_step_keyboard(
                "stock_count", extra=[[btn("⏭️ Auto (count credentials)", "pskip:stock_count", PRIMARY)]]
            ),
        )
        return

@router.callback_query(F.data == "pabort")
async def abort_wizard(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    text, markup = await _render_list(session, 1)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer("Cancelled.")


@router.callback_query(F.data.startswith("pback:"))
async def back_one_step(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Back reopens the previous step with the collected data untouched. On the first step there is
    nothing to go back to, so it behaves as Abort rather than silently doing nothing."""
    step = query.data.removeprefix("pback:")
    if step not in _PRODUCT_STEPS or _PRODUCT_STEPS.index(step) == 0:
        await abort_wizard(query, state, session)
        return

    previous = _PRODUCT_STEPS[_PRODUCT_STEPS.index(step) - 1]
    await _show_step(query.message, state, previous, session)
    await query.answer()


# ---- Wizard steps: buttons, with the typed handlers kept as a fallback ----


@router.callback_query(F.data.startswith("pickcat:"), ProductForm.category)
async def pick_category(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """"none" stores a real NULL. The old branch here fabricated an "Uncategorized" Category row,
    which then showed up as a folder in the buyer-facing store."""
    raw = query.data.removeprefix("pickcat:")
    await state.update_data(category_id=None if raw == "none" else int(raw))
    await _show_step(query.message, state, "name", session)
    await query.answer()


@router.callback_query(F.data.startswith("pmode:"), ProductForm.fulfillment_mode)
async def pick_mode(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    mode = FulfillmentMode.AUTO if query.data.endswith("auto") else FulfillmentMode.MANUAL
    await state.update_data(fulfillment_mode=mode)
    await _show_step(query.message, state, "warranty_days", session)
    await query.answer()


@router.callback_query(F.data.startswith("pwar:"), ProductForm.warranty_days)
async def pick_warranty(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    raw = query.data.removeprefix("pwar:")
    if raw == "custom":
        # Deliberately does not write a value — it waits for the admin to type one.
        await query.message.edit_text(
            f"➕ <b>Add Product</b> — step 6 of {_TOTAL_STEPS}\n\n"
            "Send the warranty length in whole days:",
            reply_markup=_step_keyboard("warranty_days"),
        )
        await query.answer()
        return
    await state.update_data(warranty_days=int(raw))
    await _show_step(query.message, state, "delivery_info", session)
    await query.answer()

@router.callback_query(F.data.startswith("pskip:"))
async def skip_step(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    field = query.data.removeprefix("pskip:")
    if field == "description":
        await state.update_data(description=None)
        await _show_step(query.message, state, "price", session)
    elif field == "delivery_info":
        await state.update_data(delivery_info=None)
        await _after_delivery_info(query.message, state, session, admin_id=query.from_user.id)
    elif field == "stock":
        await state.update_data(stock_lines=[])
        await _show_step(query.message, state, "stock_count", session)
    elif field == "stock_count":
        data = await state.get_data()
        await _finish_product(
            query.message,
            state,
            session,
            stock_lines=data.get("stock_lines", []),
            admin_id=query.from_user.id,
            manual_stock=None,
        )
    await query.answer()


async def _after_delivery_info(
    message: Message, state: FSMContext, session: AsyncSession, *, admin_id: int, edit: bool = True
) -> None:
    """MANUAL products have no stock pool, so asking for keys would be a step with no possible
    answer — they go straight to creation."""
    data = await state.get_data()
    if data.get("fulfillment_mode") is FulfillmentMode.MANUAL:
        # Straight to the count: a MANUAL product has no credentials to paste, but "how many are
        # there" is still a question worth answering, and it is the only way to give one a real
        # number instead of an unbounded IN STOCK.
        await state.update_data(stock_lines=[])
        await _show_step(message, state, "stock_count", session, edit=edit)
        return
    await _show_step(message, state, "stock", session, edit=edit)


async def _finish_product(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    stock_lines: list[str],
    admin_id: int,
    manual_stock: int | None = None,
    edit: bool = True,
) -> None:
    """The single exit from the wizard, whether stock was supplied, skipped, or never asked for."""
    data = await state.get_data()

    product_id = await create_product(
        session,
        category_id=data.get("category_id"),
        name=data["name"],
        description=data.get("description"),
        price_minor=data["price_minor"],
        currency=data["currency"],
        fulfillment_mode=data["fulfillment_mode"],
        warranty_days=data["warranty_days"],
        delivery_info=data.get("delivery_info"),
        image_file_id=None,
        manual_stock=manual_stock,
    )
    await session.flush()

    added = 0
    if stock_lines:
        added = await add_stock(
            session,
            product_id=product_id,
            plaintext_payloads=stock_lines,
            added_by_admin_id=admin_id,
        )
    await session.flush()
    await state.clear()

    if manual_stock is not None:
        tail = _manual_stock_tail(manual_stock, added)
    elif added:
        tail = f"✅ <b>LIVE</b> with {added} stock item{'s' if added != 1 else ''}."
    elif data["fulfillment_mode"] is FulfillmentMode.MANUAL:
        tail = "✅ <b>LIVE</b> — you fulfil each order by hand, so it needs no stock."
    else:
        tail = "⚠️ Shows <b>OUT OF STOCK</b> until you add stock from its product page."

    text, markup = await _render_list(session, 1)
    body = f"✅ Product <b>{data['name']}</b> created (id {product_id}).\n{tail}\n\n{text}"
    await (message.edit_text if edit else message.answer)(body, reply_markup=markup)

    # Asked, never assumed: a brand-new product is often created half-finished (no stock yet, price
    # still to check), and a product announcement is the one message every user gets.
    prompt, prompt_markup = _announce_prompt("new", product_id, data["name"])
    await message.answer(prompt, reply_markup=prompt_markup)

@router.message(Command("cancel"), ProductForm.name)
@router.message(Command("cancel"), ProductForm.description)
@router.message(Command("cancel"), ProductForm.price)
@router.message(Command("cancel"), ProductForm.fulfillment_mode)
@router.message(Command("cancel"), ProductForm.warranty_days)
@router.message(Command("cancel"), ProductForm.delivery_info)
@router.message(Command("cancel"), ProductForm.stock)
async def cancel_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


@router.message(ProductForm.name)
async def set_name(message: Message, state: FSMContext, session: AsyncSession) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 128:
        await message.answer("Please send a valid name (1-128 chars):")
        return
    await state.update_data(name=name)
    await _show_step(message, state, "description", session, edit=False)


@router.message(ProductForm.description)
async def set_description(message: Message, state: FSMContext, session: AsyncSession) -> None:
    text = (message.text or "").strip()
    await state.update_data(description=None if text.lower() == "skip" else text[:2048])
    await _show_step(message, state, "price", session, edit=False)


@router.message(ProductForm.price)
async def set_price(message: Message, state: FSMContext, session: AsyncSession) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    currency = get_settings().default_currency
    amount_text = parts[0] if parts else ""
    if len(parts) == 2:
        currency = parts[1].upper()[:8]
    try:
        price_minor = parse_to_minor(amount_text)
        if price_minor <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Please send a valid positive amount, e.g. 9.99:")
        return
    await state.update_data(price_minor=price_minor, currency=currency)
    await _show_step(message, state, "fulfillment_mode", session, edit=False)


@router.message(ProductForm.fulfillment_mode)
async def set_fulfillment(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Kept as a fallback for an admin who types out of habit — the buttons are the main path."""
    text = (message.text or "").strip().lower()
    if text not in ("auto", "manual"):
        await message.answer("Please tap ⚡ Auto or 🙋 Manual above, or send 'auto' / 'manual':")
        return
    await state.update_data(fulfillment_mode=FulfillmentMode.AUTO if text == "auto" else FulfillmentMode.MANUAL)
    await _show_step(message, state, "warranty_days", session, edit=False)

@router.message(ProductForm.warranty_days)
async def set_warranty(message: Message, state: FSMContext, session: AsyncSession) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Please send a whole number of days, or tap one of the presets above:")
        return
    await state.update_data(warranty_days=int(text))
    await _show_step(message, state, "delivery_info", session, edit=False)


@router.message(ProductForm.delivery_info)
async def set_delivery_info(
    message: Message, state: FSMContext, session: AsyncSession, user
) -> None:
    text = (message.text or "").strip()
    await state.update_data(delivery_info=None if text.lower() == "skip" else text[:2048])
    await _after_delivery_info(
        message, state, session, admin_id=user.telegram_id, edit=False
    )


@router.message(ProductForm.stock)
async def receive_wizard_stock(
    message: Message, state: FSMContext, session: AsyncSession, user
) -> None:
    payload = as_admin_wrote_it(message)
    if not payload:
        await message.answer("Send the stock item as a message — or press Skip.")
        return
    await state.update_data(stock_lines=[payload])
    await _show_step(message, state, "stock_count", session, edit=False)


@router.message(ProductForm.stock_count)
async def receive_wizard_stock_count(
    message: Message, state: FSMContext, session: AsyncSession, user
) -> None:
    count = _parse_stock_count((message.text or "").strip())
    if count is None:
        await message.answer(
            "Send a whole number 0 or greater — or press ⏭️ Auto to count credentials instead."
        )
        return
    data = await state.get_data()
    await _finish_product(
        message,
        state,
        session,
        stock_lines=data.get("stock_lines", []),
        admin_id=user.telegram_id,
        manual_stock=count,
        edit=False,
    )


# ---- Add Stock, from an existing product's page ----


async def _add_stock_screen(
    session: AsyncSession, product_id: int, *, note: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    """The Add Stock prompt. Rendered again after every batch so the form is a loop, not a one-shot.

    Stocking a product is naturally repetitive — keys arrive in batches, from different places, at
    different times. The screen used to hand the admin back to the product page after a single
    batch, which meant tapping `📦 Add Stock` again for every one. Now the prompt comes straight
    back with a running total, and `🔙 Back` is the way out.
    """
    product = await ProductRepo(session).get_by_id(product_id)
    name = product.name if product else "this product"
    in_stock = await ProductRepo(session).available_stock_count(product_id)
    return (
        f"📦 <b>Add Stock</b> — {name}\n\n"
        f"{note}"
        f"In stock now: <b>{in_stock}</b>\n\n"
        "Send the stock item — a licence key, or account credentials on as many lines as you "
        "need. <b>One message = one item</b>, so multi-line logins stay together. They are "
        "encrypted before they touch the database.\n\n"
        "Keep sending messages to add more, or press 🔙 Back when you're done.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("🔙 Back", AdminProductCB(action="view", id=str(product_id)).pack(), DANGER)]
            ]
        ),
    )


@router.callback_query(AdminProductCB.filter(F.action == "stock"))
async def start_stock(
    query: CallbackQuery, callback_data: AdminProductCB, state: FSMContext, session: AsyncSession
) -> None:
    product_id = int(callback_data.id)
    await state.set_state(StockUploadForm.payloads)
    await state.update_data(product_id=product_id)
    text, markup = await _add_stock_screen(session, product_id)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.message(Command("cancel"), StockUploadForm.payloads)
async def cancel_stock(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")

@router.message(StockUploadForm.payloads)
async def receive_stock(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    # One message = one stock item. Credentials are routinely multi-line (login, password, 2FA
    # code, notes), so splitting on newlines would shred a single account into several unusable
    # "items". The whole message body is kept verbatim, minus surrounding blank space — including
    # its formatting, so a credential the admin sent as a copy-box arrives as one.
    payload = as_admin_wrote_it(message)
    if not payload:
        await message.answer("Send the stock item as a message, or /cancel:")
        return
    lines = [payload]

    data = await state.get_data()
    product_id = data["product_id"]
    # Read the latch *before* add_stock clears it: that column is the only record that this product
    # had sold out, and it is what tells a restock apart from topping up a shelf that never emptied.
    before = await ProductRepo(session).get_by_id(product_id)
    # A product that has never held a single item isn't coming *back* — this is its first stocking,
    # and it was already offered as a new-product announcement when it was created.
    ever_stocked = await session.scalar(
        select(func.count()).select_from(StockItem).where(StockItem.product_id == product_id)
    )
    was_sold_out = (
        before is not None and before.status is ProductStatus.OUT_OF_STOCK and bool(ever_stocked)
    )
    try:
        count = await add_stock(
            session,
            product_id=product_id,
            plaintext_payloads=lines,
            added_by_admin_id=user.telegram_id,
        )
    except ValueError as exc:
        # Stay in the form: a rejected batch is something to retype, not a reason to walk the admin
        # back to the product page and make them start the whole flow again.
        await message.answer(f"❌ {exc}\n\nSend the items again, or press 🔙 Back to stop.")
        return

    await session.flush()
    # The state deliberately survives, so the very next message adds another batch.
    text, markup = await _add_stock_screen(
        session, product_id, note=f"✅ Added <b>{count}</b> item(s).\n"
    )
    await message.answer(text, reply_markup=markup)

    # Only on the batch that actually ends the drought — the loop stays open afterwards, so asking
    # again on every further batch would nag the admin for one restock.
    if was_sold_out and before is not None:
        prompt, prompt_markup = _announce_prompt("restock", product_id, before.name)
        await message.answer(prompt, reply_markup=prompt_markup)


# ---- Announcements ----
#
# Two of the three announcement kinds are opt-in, and both ask here. The third — a sell-out — never
# reaches this screen: it fires by itself from the checkout path (announcement_service
# .maybe_announce_sold_out), because the admin isn't present when a buyer takes the last item.

_ANNOUNCE_HEADLINE = {
    "new": "🆕 Announce this as a <b>new product</b>?",
    "restock": "🔄 Announce this as <b>back in stock</b>?",
}


def _announce_prompt(kind: str, product_id: int, name: str) -> tuple[str, InlineKeyboardMarkup]:
    return (
        f"{_ANNOUNCE_HEADLINE[kind]}\n\n"
        f"<b>{name}</b>\n\n"
        "Announce sends it to <b>every user</b> as a broadcast. Decline changes nothing — the "
        "product stays exactly as it is, just unannounced.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    btn("📢 Announce", f"pann:{kind}:{product_id}", SUCCESS),
                    btn("🚫 Decline", "panndecl", DANGER),
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("pann:"))
async def send_product_announcement(query: CallbackQuery, session: AsyncSession, user) -> None:
    _, kind, raw_id = query.data.split(":", 2)
    product = await ProductRepo(session).get_by_id(int(raw_id))
    if product is None:
        await query.answer("That product is gone.", show_alert=True)
        return

    targets = await (
        announce_new_product if kind == "new" else announce_restock
    )(query.message.bot, session, product, user.telegram_id)

    await query.message.edit_text(
        f"📢 Announcing <b>{product.name}</b> to {targets} user(s)…",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("🔙 Back", AdminProductCB(action="view", id=str(product.id)).pack(), PRIMARY)]
            ]
        ),
    )
    await query.answer()


@router.callback_query(F.data == "panndecl")
async def decline_product_announcement(query: CallbackQuery) -> None:
    await query.message.edit_text("🚫 Not announced.")
    await query.answer()


# ---- Search ----


@router.callback_query(AdminProductCB.filter(F.action == "search"))
async def start_search(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProductSearchForm.term)
    await query.message.edit_text(
        "🔍 <b>Search products</b>\n\n"
        "Send part of a product name — matching is case-insensitive.\n\n"
        "Send <code>*</code> to clear the filter and see everything again.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[nav_row("en", back_target="admin_panel", home=False)]
        ),
    )
    await query.answer()


@router.message(ProductSearchForm.term)
async def apply_search(message: Message, state: FSMContext, session: AsyncSession) -> None:
    term = (message.text or "").strip()
    term = None if term == "*" else term
    await state.clear()
    # Survives state.clear() deliberately: the filter has to outlive the search form so that paging
    # the filtered list keeps it applied.
    await state.update_data(product_filter=term)
    text, markup = await _render_list(session, 1, name_like=term)
    await message.answer(text, reply_markup=markup)

# ---- CSV import and export ----


@router.callback_query(AdminProductCB.filter(F.action == "import"))
async def start_import(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProductImportForm.document)
    await query.message.edit_text(
        "📥 <b>Import products from CSV</b>\n\n"
        "Upload a <code>.csv</code> file with this header:\n\n"
        "<code>id,name,category,price,currency,mode,warranty,description,delivery_info,active</code>\n\n"
        "Only <b>name</b> and <b>price</b> are required. A blank <b>category</b> files the product "
        "outside every folder; a category that does not exist yet is created.\n\n"
        "Rows with an <b>id</b> update that product. Rows without one update a product of the same "
        "name, or create it.\n\n"
        "Stock is <b>not</b> imported — add licence keys per product afterwards, so a quoting "
        "mistake can never ship the wrong key to a buyer.\n\n"
        f"Limits: {MAX_ROWS} rows, {MAX_BYTES // 1000} KB.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("📤 Export current products", AdminProductCB(action="export").pack(), PRIMARY)],
                nav_row("en", back_target="admin_panel", home=False),
            ]
        ),
    )
    await query.answer()


@router.message(ProductImportForm.document, F.document)
async def receive_import(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.document.file_size and message.document.file_size > MAX_BYTES:
        await message.answer(f"❌ File is larger than {MAX_BYTES // 1000} KB.")
        return

    buffer = await message.bot.download(message.document)
    try:
        text = buffer.read().decode("utf-8")
    except UnicodeDecodeError:
        await message.answer("❌ The file is not UTF-8 text. Re-save it as CSV UTF-8.")
        return

    try:
        parsed = parse_csv(text, default_currency=get_settings().default_currency)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    report = await apply_rows(session, parsed.rows)
    await state.clear()
    await session.flush()

    problems = parsed.errors + report.errors
    lines = [
        "✅ <b>Import complete</b>",
        f"   {report.created} created · {report.updated} updated · {len(problems)} error(s)",
    ]
    if report.categories_created:
        lines.append(f"   Categories created: {', '.join(report.categories_created)}")
    if problems:
        lines.append("")
        lines.extend(f"❌ {p}" for p in problems[:20])
        if len(problems) > 20:
            lines.append(f"…and {len(problems) - 20} more.")

    await message.answer("\n".join(lines))

@router.message(ProductImportForm.document)
async def reject_non_document(message: Message) -> None:
    """The state is waiting on a file. Without this, typed text falls through to whatever other
    handler matches next and the admin gets a confusing screen instead of an answer."""
    await message.answer("❌ Send the CSV as a <b>file attachment</b>, not as text.")


@router.callback_query(AdminProductCB.filter(F.action == "export"))
async def export_products(query: CallbackQuery, session: AsyncSession) -> None:
    products = list((await session.execute(select(Product).order_by(Product.id))).scalars().all())
    categories = list((await session.execute(select(Category))).scalars().all())
    text = to_csv(products, {c.id: c.name for c in categories})

    await query.message.answer_document(
        BufferedInputFile(text.encode("utf-8"), filename="products.csv"),
        caption=(
            f"📤 {len(products)} product(s). Edit the file and re-upload it via 📥 Import CSV — "
            "rows keep their id, so nothing is duplicated."
        ),
    )
    await query.answer()

# ---- Per-product edit ----


@router.callback_query(AdminProductCB.filter(F.action == "edit"))
async def choose_edit_field(
    query: CallbackQuery, callback_data: AdminProductCB, session: AsyncSession, state: FSMContext
) -> None:
    """Kept only so older messages still in a chat don't dead-end on a stale `edit` button. The
    field picker itself is gone — the fields are on the product screen now."""
    await view_product(query, callback_data, session, state)


_EDIT_PROMPTS = {
    "nm": "Send the new product name (1-128 characters):",
    "pr": "Send the new price, e.g. <code>9.99</code>. Add a currency to change it: <code>9.99 EUR</code>",
    "ds": "Send the new description:",
    "dv": "Send the new delivery instructions shown to buyers after purchase:",
}


@router.callback_query(F.data.startswith("pedit:"))
async def prompt_edit_field(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    _, code, product_id = query.data.split(":", 2)

    def _cancel_row() -> list[InlineKeyboardButton]:
        return [btn("🔙 Back", AdminProductCB(action="view", id=product_id).pack(), DANGER)]

    if code == "md":
        # Which mode is live is the first thing you want to know on this screen — it is stated in
        # the body and ticked on the button, so the choice reads as "change it to" rather than
        # "pick one of these two, whichever you're already on".
        product = await ProductRepo(session).get_by_id(int(product_id))
        is_auto = product is not None and product.fulfillment_mode is FulfillmentMode.AUTO
        current = "⚡ Auto" if is_auto else "🙋 Manual"
        credentials = (
            await ProductRepo(session).available_stock_count(product.id) if product else 0
        )
        await query.message.edit_text(
            "✏️ <b>Fulfillment mode</b>\n\n"
            f"Currently: <b>{current}</b> · <b>{credentials}</b> credential(s) loaded\n\n"
            "⚡ <b>Auto</b> — a stock item is sent the moment payment clears. Switching to Auto "
            f"resets the stock count to the credentials you have loaded (<b>{credentials}</b>).\n"
            "🙋 <b>Manual</b> — the order lands in your queue and you fulfil it yourself. Your "
            "credentials are frozen: nothing is delivered from the pool and nothing is consumed, "
            "whatever you set the stock count to.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        btn(
                            "✅ ⚡ Auto" if is_auto else "⚡ Auto",
                            f"pedset:md:auto:{product_id}",
                            SUCCESS if is_auto else PRIMARY,
                        ),
                        btn(
                            "✅ 🙋 Manual" if not is_auto else "🙋 Manual",
                            f"pedset:md:manual:{product_id}",
                            PRIMARY if is_auto else SUCCESS,
                        ),
                    ],
                    _cancel_row(),
                ]
            ),
        )
        await query.answer()
        return

    if code == "wr":
        await query.message.edit_text(
            "✏️ <b>Warranty</b>\n\nHow long can a buyer file a claim after purchase?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        btn("None", f"pedset:wr:0:{product_id}", PRIMARY),
                        btn("7d", f"pedset:wr:7:{product_id}", PRIMARY),
                        btn("30d", f"pedset:wr:30:{product_id}", PRIMARY),
                    ],
                    [
                        btn("90d", f"pedset:wr:90:{product_id}", PRIMARY),
                        btn("365d", f"pedset:wr:365:{product_id}", PRIMARY),
                    ],
                    _cancel_row(),
                ]
            ),
        )
        await query.answer()
        return

    if code == "ct":
        categories = await CategoryRepo(session).list_active()
        rows = [
            [btn(f"{c.emoji or '📦'} {c.name}", f"pedset:ct:{c.id}:{product_id}", PRIMARY)]
            for c in categories
        ]
        rows.append([btn("🚫 No category", f"pedset:ct:none:{product_id}", PRIMARY)])
        rows.append(_cancel_row())
        await query.message.edit_text(
            "✏️ <b>Category</b>\n\nWhere should this product live?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await query.answer()
        return

    if code == "st":
        # Typed like a text field, but with one button — "back to automatic" is not a number the
        # admin could express by typing, so it has to be its own control.
        product = await ProductRepo(session).get_by_id(int(product_id))
        if product is None:
            await query.answer("Product not found.", show_alert=True)
            return
        credentials = await ProductRepo(session).available_stock_count(product.id)
        current = (
            f"<b>{product.manual_stock}</b> (set by hand)"
            if product.manual_stock is not None
            else f"<b>{credentials}</b> (automatic — counting credentials)"
        )
        await state.set_state(ProductEditForm.value)
        await state.update_data(edit_product_id=int(product_id), edit_field=code)
        await query.message.edit_text(
            f"✏️ <b>Stock count</b>\n\nCurrently: {current}\n"
            f"Credentials loaded: <b>{credentials}</b>\n\n{_STOCK_COUNT_HELP}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [btn("⏭️ Auto (count credentials)", f"pedset:st:auto:{product_id}", PRIMARY)],
                    _cancel_row(),
                ]
            ),
        )
        await query.answer()
        return

    if code not in _EDIT_PROMPTS:
        await query.answer("Unknown field.", show_alert=True)
        return

    await state.set_state(ProductEditForm.value)
    await state.update_data(edit_product_id=int(product_id), edit_field=code)
    await query.message.edit_text(
        f"✏️ <b>{_EDIT_FIELDS[code]}</b>\n\n{_EDIT_PROMPTS[code]}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[_cancel_row()]),
    )
    await query.answer()


@router.callback_query(F.data.startswith("pedset:"))
async def apply_edit_button(query: CallbackQuery, session: AsyncSession) -> None:
    """The closed-set fields save straight from the button — no typing, no confirmation step."""
    _, code, value, raw_id = query.data.split(":", 3)
    product = await ProductRepo(session).get_by_id(int(raw_id))
    if product is None:
        await query.answer("Product not found.", show_alert=True)
        return

    if code == "md":
        product.fulfillment_mode = FulfillmentMode.AUTO if value == "auto" else FulfillmentMode.MANUAL
        if value == "auto":
            # Going back to Auto hands counting back to the credential pool. Keeping the old
            # hand-set number would be the worse surprise of the two: it was typed for a product
            # that wasn't delivering credentials, and it would now cap or overshoot a shelf it was
            # never about. The pool itself was frozen while MANUAL was on, so this lands on exactly
            # the number of keys that are actually loaded.
            product.manual_stock = None
    elif code == "wr":
        product.warranty_days = int(value)
    elif code == "ct":
        product.category_id = None if value == "none" else int(value)
    elif code == "st":
        product.manual_stock = None
    await _resync_status(session, product)
    await session.flush()

    rendered = await _render_detail(session, product.id)
    text, markup = rendered
    await query.message.edit_text(f"✅ {_EDIT_FIELDS[code]} updated.\n\n{text}", reply_markup=markup)
    await query.answer("Saved.")

@router.message(Command("cancel"), ProductEditForm.value)
async def cancel_edit(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


@router.message(ProductEditForm.value)
async def apply_edit_typed(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    product = await ProductRepo(session).get_by_id(data["edit_product_id"])
    if product is None:
        await state.clear()
        await message.answer("❌ That product no longer exists.")
        return

    code = data["edit_field"]
    text = (message.text or "").strip()

    if code == "nm":
        if not text or len(text) > 128:
            await message.answer("Please send a valid name (1-128 chars):")
            return
        product.name = text
    elif code == "pr":
        parts = text.split()
        try:
            price_minor = parse_to_minor(parts[0] if parts else "")
            if price_minor <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Please send a valid positive amount, e.g. 9.99:")
            return
        product.price_minor = price_minor
        if len(parts) == 2:
            product.currency = parts[1].upper()[:8]
    elif code == "ds":
        product.description = text[:2048] or None
    elif code == "dv":
        product.delivery_info = text[:2048] or None
    elif code == "st":
        count = _parse_stock_count(text)
        if count is None:
            await message.answer(
                "Send a whole number 0 or greater — or press ⏭️ Auto to count credentials instead."
            )
            return
        product.manual_stock = count

    await _resync_status(session, product)
    await session.flush()
    await state.clear()

    detail, markup = await _render_detail(session, product.id)
    await message.answer(f"✅ {_EDIT_FIELDS[code]} updated.\n\n{detail}", reply_markup=markup)
