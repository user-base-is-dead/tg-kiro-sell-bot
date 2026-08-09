from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminProductCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.styles import NEUTRAL, PRIMARY, btn
from app.bot.states.product_form import ProductForm, StockUploadForm
from app.database.models.catalog import FulfillmentMode
from app.database.repositories.category_repo import CategoryRepo
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import add_stock, compute_display_status, create_product
from app.utils.money import format_minor, parse_to_minor
from app.utils.pagination import Page
from app.utils.status_emoji import STATUS_EMOJI

router = Router(name="admin.products")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

PAGE_SIZE = 10


def _list_keyboard(products: list, page: Page) -> InlineKeyboardMarkup:
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
    rows.append([btn("➕ Add Product", AdminProductCB(action="add").pack(), PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detail_keyboard(product) -> InlineKeyboardMarkup:
    toggle_label = "🔴 Disable" if product.is_active else "🟢 Enable"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📦 Add Stock", AdminProductCB(action="stock", id=str(product.id)).pack(), PRIMARY)],
            [
                btn(
                    toggle_label,
                    AdminProductCB(action="toggle", id=str(product.id)).pack(),
                    PRIMARY if product.is_active else PRIMARY,
                )
            ],
            [btn("🗑️ Delete", AdminProductCB(action="delete", id=str(product.id)).pack(), PRIMARY)],
            [btn("📙 Back", AdminProductCB(action="list").pack(), PRIMARY)],
        ]
    )


async def _render_list(session: AsyncSession, page_num: int) -> tuple[str, InlineKeyboardMarkup]:
    from sqlalchemy import select

    from app.database.models.catalog import Product

    total_result = await session.execute(select(Product))
    all_products = list(total_result.scalars().all())
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=len(all_products))
    products = all_products[page.offset : page.offset + PAGE_SIZE]
    text = f"📦 <b>PRODUCT MANAGEMENT</b>\n\n{len(all_products)} products total."
    return text, _list_keyboard(products, page)


@router.callback_query(AdminProductCB.filter(F.action == "list"))
async def list_products(query: CallbackQuery, callback_data: AdminProductCB, session: AsyncSession) -> None:
    text, markup = await _render_list(session, callback_data.page)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(AdminProductCB.filter(F.action == "view"))
async def view_product(query: CallbackQuery, callback_data: AdminProductCB, session: AsyncSession) -> None:
    product = await ProductRepo(session).get_by_id(int(callback_data.id))
    if product is None:
        await query.answer("Product not found.", show_alert=True)
        return
    view = await compute_display_status(session, product)
    text = (
        f"{STATUS_EMOJI[view.display_status]} <b>{product.name}</b>\n\n"
        f"Price: {format_minor(product.price_minor, product.currency)}\n"
        f"Stock: {view.available_stock}\n"
        f"Fulfillment: {product.fulfillment_mode.value}\n"
        f"Warranty: {product.warranty_days} days\n"
        f"Description: {product.description or '—'}"
    )
    await query.message.edit_text(text, reply_markup=_detail_keyboard(product))
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
async def delete_product(query: CallbackQuery, callback_data: AdminProductCB, session: AsyncSession) -> None:
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
    text, markup = await _render_list(session, 1)
    await query.message.edit_text(text, reply_markup=markup)


@router.callback_query(AdminProductCB.filter(F.action == "add"))
async def start_add(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    categories = await CategoryRepo(session).list_active()
    rows = [[btn(f"{c.emoji or '📦'} {c.name}", f"pickcat:{c.id}", PRIMARY)] for c in categories]
    rows.append([btn("🚫 No category", "pickcat:none", PRIMARY)])
    await state.set_state(ProductForm.category)
    await query.message.edit_text("➕ <b>Add Product</b>\n\nChoose a category:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()


@router.callback_query(F.data.startswith("pickcat:"), ProductForm.category)
async def pick_category(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """"none" stores a real NULL. The old branch here fabricated an "Uncategorized" Category row,
    which then showed up as a folder in the buyer-facing store."""
    raw = query.data.removeprefix("pickcat:")
    await state.update_data(category_id=None if raw == "none" else int(raw))
    await state.set_state(ProductForm.name)
    await query.message.edit_text("Send the product name (or /cancel):")
    await query.answer()


@router.message(Command("cancel"), ProductForm.name)
@router.message(Command("cancel"), ProductForm.description)
@router.message(Command("cancel"), ProductForm.price)
@router.message(Command("cancel"), ProductForm.fulfillment_mode)
@router.message(Command("cancel"), ProductForm.warranty_days)
@router.message(Command("cancel"), ProductForm.delivery_info)
async def cancel_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


@router.message(ProductForm.name)
async def set_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 128:
        await message.answer("Please send a valid name (1-128 chars):")
        return
    await state.update_data(name=name)
    await state.set_state(ProductForm.description)
    await message.answer("Send a description (or 'skip'):")


@router.message(ProductForm.description)
async def set_description(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    await state.update_data(description=None if text.lower() == "skip" else text[:2048])
    await state.set_state(ProductForm.price)
    await message.answer("Send the price, e.g. 9.99 (USD assumed unless you write '9.99 EUR'):")


@router.message(ProductForm.price)
async def set_price(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    currency = "USD"
    amount_text = parts[0]
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
    await state.set_state(ProductForm.fulfillment_mode)
    await message.answer("Fulfillment mode — send 'auto' (instant delivery) or 'manual' (admin fulfills):")


@router.message(ProductForm.fulfillment_mode)
async def set_fulfillment(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().lower()
    if text not in ("auto", "manual"):
        await message.answer("Please send 'auto' or 'manual':")
        return
    await state.update_data(fulfillment_mode=FulfillmentMode.AUTO if text == "auto" else FulfillmentMode.MANUAL)
    await state.set_state(ProductForm.warranty_days)
    await message.answer("Warranty duration in days (0 for none):")


@router.message(ProductForm.warranty_days)
async def set_warranty(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Please send a whole number of days (0 for none):")
        return
    await state.update_data(warranty_days=int(text))
    await state.set_state(ProductForm.delivery_info)
    await message.answer("Delivery info shown to buyers after purchase (or 'skip'):")


@router.message(ProductForm.delivery_info)
async def set_delivery_info(message: Message, state: FSMContext, session: AsyncSession) -> None:
    text = (message.text or "").strip()
    delivery_info = None if text.lower() == "skip" else text[:2048]
    data = await state.get_data()

    product_id = await create_product(
        session,
        category_id=data["category_id"],
        name=data["name"],
        description=data.get("description"),
        price_minor=data["price_minor"],
        currency=data["currency"],
        fulfillment_mode=data["fulfillment_mode"],
        warranty_days=data["warranty_days"],
        delivery_info=delivery_info,
        image_file_id=None,
    )
    await session.flush()
    await state.clear()
    await message.answer(
        f"✅ Product <b>{data['name']}</b> created (id {product_id}).\n"
        f"It shows OUT OF STOCK until you add stock via Manage Stock."
    )


@router.callback_query(AdminProductCB.filter(F.action == "stock"))
async def start_stock(query: CallbackQuery, callback_data: AdminProductCB, state: FSMContext) -> None:
    await state.set_state(StockUploadForm.payloads)
    await state.update_data(product_id=int(callback_data.id))
    await query.message.edit_text(
        "📦 <b>Add Stock</b>\n\nSend stock items, one per line (e.g. license keys/account details). /cancel to abort."
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
        await message.answer(f"✅ Added {count} stock items.")
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
