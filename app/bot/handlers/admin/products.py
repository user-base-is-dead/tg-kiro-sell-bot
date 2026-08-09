from __future__ import annotations

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
from sqlalchemy import select
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
from app.database.models.catalog import Category, FulfillmentMode, Product
from app.database.repositories.category_repo import CategoryRepo
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import add_stock, compute_display_status, create_product
from app.services.product_import import MAX_BYTES, MAX_ROWS, apply_rows, parse_csv, to_csv
from app.utils.money import format_minor, parse_to_minor
from app.utils.pagination import Page
from app.utils.status_emoji import STATUS_EMOJI

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
}

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


def _list_keyboard(products: list, page: Page, *, name_like: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    for p in products:
        dot = "🟢" if p.is_active else "⚫"
        rows.append(
            [
                btn(
                    f"{dot} {p.name} — {format_minor(p.price_minor, p.currency)}",
                    AdminProductCB(action="view", id=str(p.id)).pack(),
                    PRIMARY if p.is_active else NEUTRAL,
                )
            ]
        )
    nav = []
    if page.has_prev:
        nav.append(btn("◀️", AdminProductCB(action="list", page=page.clamped_page - 1).pack(), PRIMARY))
    if page.total_pages > 1:
        nav.append(btn(f"{page.clamped_page}/{page.total_pages}", "noop", NEUTRAL))
    if page.has_next:
        nav.append(btn("▶️", AdminProductCB(action="list", page=page.clamped_page + 1).pack(), PRIMARY))
    if nav:
        rows.append(nav)

    rows.append([btn("➕ Add Product", AdminProductCB(action="add").pack(), SUCCESS)])
    rows.append(
        [
            btn("📥 Import CSV", AdminProductCB(action="import").pack(), PRIMARY),
            btn("📤 Export CSV", AdminProductCB(action="export").pack(), PRIMARY),
        ]
    )
    # The search button doubles as the way out of a filter, so its label reflects the active one.
    search_label = f"🔍 Filtered: {name_like}" if name_like else "🔍 Search"
    rows.append([btn(search_label, AdminProductCB(action="search").pack(), PRIMARY)])
    rows.append(nav_row("en", back_target="admin_panel", home=False))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _detail_keyboard(product) -> InlineKeyboardMarkup:
    toggle_label = "🔴 Disable" if product.is_active else "🟢 Enable"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✏️ Edit", AdminProductCB(action="edit", id=str(product.id)).pack(), PRIMARY)],
            [btn("📦 Add Stock", AdminProductCB(action="stock", id=str(product.id)).pack(), PRIMARY)],
            [
                btn(
                    toggle_label,
                    AdminProductCB(action="toggle", id=str(product.id)).pack(),
                    DANGER if product.is_active else SUCCESS,
                )
            ],
            [btn("🗑️ Delete", AdminProductCB(action="delete", id=str(product.id)).pack(), DANGER)],
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


async def _render_list(
    session: AsyncSession, page_num: int, *, name_like: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    total = await _count_products(session, name_like=name_like)
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=total)
    # Nothing to fetch when the count is zero, so the page query is skipped entirely.
    products = (
        await _list_page(session, offset=page.offset, limit=PAGE_SIZE, name_like=name_like)
        if total
        else []
    )

    filter_line = f"🔍 Filtered by “{name_like}” — tap the filter button to clear it.\n" if name_like else ""
    text = (
        "📦 <b>PRODUCT MANAGEMENT</b>\n\n"
        f"{total} product(s) total.\n{filter_line}\n"
        "Tap any product to see its stock, price and status, or to edit, disable or delete it.\n\n"
        "<b>Buttons:</b>\n"
        "➕ <b>Add Product</b> — walk through creating one product, stock included\n"
        "📥 <b>Import CSV</b> — upload a file to create or update products in bulk\n"
        "📤 <b>Export CSV</b> — download every product in that same format, edit it, re-upload\n"
        "🔍 <b>Search</b> — filter this list by name\n\n"
        "🟢 = active and visible to buyers · ⚫ = disabled and hidden"
    )
    return text, _list_keyboard(products, page, name_like=name_like)

async def _render_detail(session: AsyncSession, product_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    product = await ProductRepo(session).get_by_id(product_id)
    if product is None:
        return None

    view = await compute_display_status(session, product)
    category = "🚫 none" if product.category_id is None else "—"
    if product.category_id is not None:
        found = await CategoryRepo(session).get_by_id(product.category_id)
        category = found.name if found else "—"

    stock_line = (
        "Fulfilled by hand — no stock pool"
        if product.fulfillment_mode is FulfillmentMode.MANUAL
        else str(view.available_stock)
    )
    text = (
        f"{STATUS_EMOJI[view.display_status]} <b>{product.name}</b>\n\n"
        f"Price: {format_minor(product.price_minor, product.currency)}\n"
        f"Category: {category}\n"
        f"Stock: {stock_line}\n"
        f"Fulfillment: {product.fulfillment_mode.value}\n"
        f"Warranty: {product.warranty_days} days\n"
        f"Description: {product.description or '—'}\n"
        f"Delivery info: {product.delivery_info or '—'}"
    )
    return text, _detail_keyboard(product)


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


@router.callback_query(AdminProductCB.filter(F.action == "view"))
async def view_product(query: CallbackQuery, callback_data: AdminProductCB, session: AsyncSession) -> None:
    rendered = await _render_detail(session, int(callback_data.id))
    if rendered is None:
        await query.answer("Product not found.", show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()

@router.callback_query(AdminProductCB.filter(F.action == "toggle"))
async def toggle_product(query: CallbackQuery, callback_data: AdminProductCB, session: AsyncSession) -> None:
    product = await ProductRepo(session).get_by_id(int(callback_data.id))
    if product is None:
        await query.answer("Product not found.", show_alert=True)
        return
    product.is_active = not product.is_active
    await session.flush()
    await view_product(query, callback_data, session)


@router.callback_query(AdminProductCB.filter(F.action == "delete"))
async def delete_product(
    query: CallbackQuery, callback_data: AdminProductCB, state: FSMContext, session: AsyncSession
) -> None:
    product = await ProductRepo(session).get_by_id(int(callback_data.id))
    if product is None:
        await query.answer("Product not found.", show_alert=True)
        return
    try:
        await ProductRepo(session).delete(product)
        await session.flush()
    except IntegrityError:
        await session.rollback()
        await query.answer("Can't delete — this product has order history. Disable it instead.", show_alert=True)
        return
    await query.answer("Deleted.")
    name_like = (await state.get_data()).get("product_filter")
    text, markup = await _render_list(session, 1, name_like=name_like)
    await query.message.edit_text(text, reply_markup=markup)


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
            f"{head}Send the stock items — licence keys or account credentials, <b>one per line</b>. "
            "They are encrypted before they touch the database.\n\n"
            "Skip to create the product OUT OF STOCK and add them later.",
            reply_markup=_step_keyboard("stock", extra=[[btn("⏭️ Skip", "pskip:stock", PRIMARY)]]),
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
        await _finish_product(
            query.message, state, session, stock_lines=[], admin_id=query.from_user.id
        )
    await query.answer()


async def _after_delivery_info(
    message: Message, state: FSMContext, session: AsyncSession, *, admin_id: int, edit: bool = True
) -> None:
    """MANUAL products have no stock pool, so asking for keys would be a step with no possible
    answer — they go straight to creation."""
    data = await state.get_data()
    if data.get("fulfillment_mode") is FulfillmentMode.MANUAL:
        await _finish_product(message, state, session, stock_lines=[], admin_id=admin_id, edit=edit)
        return
    await _show_step(message, state, "stock", session, edit=edit)


async def _finish_product(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    stock_lines: list[str],
    admin_id: int,
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

    if added:
        tail = f"✅ <b>LIVE</b> with {added} stock item{'s' if added != 1 else ''}."
    elif data["fulfillment_mode"] is FulfillmentMode.MANUAL:
        tail = "✅ <b>LIVE</b> — you fulfil each order by hand, so it needs no stock."
    else:
        tail = "⚠️ Shows <b>OUT OF STOCK</b> until you add stock from its product page."

    text, markup = await _render_list(session, 1)
    body = f"✅ Product <b>{data['name']}</b> created (id {product_id}).\n{tail}\n\n{text}"
    await (message.edit_text if edit else message.answer)(body, reply_markup=markup)

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
    lines = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
    if not lines:
        await message.answer("Send at least one stock item, one per line — or press Skip.")
        return
    await _finish_product(
        message, state, session, stock_lines=lines, admin_id=user.telegram_id, edit=False
    )


# ---- Add Stock, from an existing product's page ----


@router.callback_query(AdminProductCB.filter(F.action == "stock"))
async def start_stock(query: CallbackQuery, callback_data: AdminProductCB, state: FSMContext) -> None:
    await state.set_state(StockUploadForm.payloads)
    await state.update_data(product_id=int(callback_data.id))
    await query.message.edit_text(
        "📦 <b>Add Stock</b>\n\n"
        "Send the stock items — licence keys or account credentials, <b>one per line</b>. "
        "Paste as many as you like in a single message; they are encrypted before they touch "
        "the database.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("🔙 Back", AdminProductCB(action="view", id=callback_data.id).pack(), DANGER)]
            ]
        ),
    )
    await query.answer()


@router.message(Command("cancel"), StockUploadForm.payloads)
async def cancel_stock(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")

@router.message(StockUploadForm.payloads)
async def receive_stock(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    text = message.text or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        await message.answer("Send at least one non-empty line, or /cancel:")
        return

    data = await state.get_data()
    product_id = data["product_id"]
    try:
        count = await add_stock(
            session,
            product_id=product_id,
            plaintext_payloads=lines,
            added_by_admin_id=user.telegram_id,
        )
        await state.clear()
        await session.flush()
        rendered = await _render_detail(session, product_id)
        if rendered is None:
            await message.answer(f"✅ Added {count} stock items.")
            return
        detail, markup = rendered
        await message.answer(f"✅ Added {count} stock item(s).\n\n{detail}", reply_markup=markup)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")


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
async def choose_edit_field(query: CallbackQuery, callback_data: AdminProductCB) -> None:
    rows = [
        [btn(f"✏️ {label}", f"pedit:{code}:{callback_data.id}", PRIMARY)]
        for code, label in _EDIT_FIELDS.items()
    ]
    rows.append([btn("🔙 Back", AdminProductCB(action="view", id=callback_data.id).pack(), DANGER)])
    await query.message.edit_text(
        "✏️ <b>Edit product</b>\n\nWhich field? The change saves as soon as you answer.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await query.answer()


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
        return [btn("🔙 Back", AdminProductCB(action="edit", id=product_id).pack(), DANGER)]

    if code == "md":
        await query.message.edit_text(
            "✏️ <b>Fulfillment mode</b>\n\n"
            "⚡ <b>Auto</b> — a stock item is sent the moment payment clears.\n"
            "🙋 <b>Manual</b> — the order lands in your queue and you fulfil it.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        btn("⚡ Auto", f"pedset:md:auto:{product_id}", PRIMARY),
                        btn("🙋 Manual", f"pedset:md:manual:{product_id}", PRIMARY),
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
    elif code == "wr":
        product.warranty_days = int(value)
    elif code == "ct":
        product.category_id = None if value == "none" else int(value)
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

    await session.flush()
    await state.clear()

    detail, markup = await _render_detail(session, product.id)
    await message.answer(f"✅ {_EDIT_FIELDS[code]} updated.\n\n{detail}", reply_markup=markup)
