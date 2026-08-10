from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import CategoryCB, ProductCB
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.products import category_grid, product_detail, product_list
from app.database.models.user import User
from app.database.repositories.category_repo import CategoryRepo
from app.database.repositories.product_repo import ProductRepo
from app.locales.i18n import t
from app.services.catalog_service import compute_display_status
from app.services import order_hold_service
from app.utils.pagination import Page
from app.utils.status_emoji import STATUS_EMOJI, STATUS_LABEL
from app.utils.text import PAD

router = Router(name="products.browse")

PAGE_SIZE = 12


async def render_categories(session: AsyncSession, locale: str) -> tuple[str, object]:
    categories = await CategoryRepo(session).list_active()
    # Products with no category are real stock, not an empty store — they render above the folders.
    loose = await ProductRepo(session).list_uncategorized()
    if not categories and not loose:
        return "🛍️ <b>STORE</b>\n\n💳 Premium products available now! Pay with crypto (💎 USDT/BNB) and get instant delivery. Browse categories or visit /products to see all available items.", category_grid([], locale)
    heading = "Choose a category:" if categories else "Available now:"
    return f"🛍️ <b>STORE</b>\n\n{heading}\n{PAD}", category_grid(categories, locale, loose=loose)


async def render_product_list(
    session: AsyncSession, category_id: int, page_num: int, locale: str
) -> tuple[str, object] | None:
    category = await CategoryRepo(session).get_by_id(category_id)
    if category is None:
        return None

    repo = ProductRepo(session)
    total = await repo.count_by_category(category_id)
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=total)
    products = await repo.list_by_category(category_id, offset=page.offset, limit=PAGE_SIZE)
    views = [await compute_display_status(session, p) for p in products]

    emoji = category.emoji or "🛍️"
    if not views:
        text = f"{emoji} <b>{category.name.upper()}</b>\n\nNo products in this category yet."
    else:
        lines = "\n".join(
            f"{STATUS_EMOJI[v.display_status]} {v.product.name} — "
            f"{v.product.price_minor / 100:.2f} {v.product.currency}"
            for v in views
        )
        text = f"{emoji} <b>{category.name.upper()}</b>\n\n{lines}"

    return f"{text}\n{PAD}", product_list(views, category_id, page, locale)


async def render_product_detail(session: AsyncSession, product_id: int, locale: str, user_id: int | None = None) -> tuple[str, object] | None:
    product = await ProductRepo(session).get_by_id(product_id)
    if product is None or not product.is_active:
        return None

    view = await compute_display_status(session, product)
    status_line = f"{STATUS_EMOJI[view.display_status]} {STATUS_LABEL[view.display_status]}"
    if view.display_status.value == "LOW_STOCK":
        status_line += f"\nOnly {view.available_stock} remaining!"

    # Check for product hold
    hold = await order_hold_service.get_hold_on_product(session, product_id)
    hold_line = ""
    if hold is not None:
        remaining = (hold.expires_at - __import__('datetime').datetime.now(__import__('datetime').UTC)).total_seconds()
        if remaining > 0:
            minutes = int(remaining) // 60
            seconds = int(remaining) % 60
            hold_line = f"\n⏱️ <b>On hold for {minutes}m {seconds}s</b>"
            if user_id and hold.user_id == user_id:
                hold_line = f"\n🔒 <b>Your payment: {minutes}m {seconds}s remaining</b>"

    stock_line = "" if product.fulfillment_mode.value == "MANUAL" else f"📦 Stock: {view.available_stock}\n"
    text = (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🛍️ <b>{product.name.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{product.description or 'Premium digital product.'}\n\n"
        f"💰 Price: {product.price_minor / 100:.2f} {product.currency}\n"
        f"{stock_line}"
        + (f"🛡️ Warranty: {product.warranty_days} Days\n" if product.warranty_days else "")
        + f"\n{status_line}{hold_line}"
        + f"\n{PAD}"
    )
    return text, product_detail(product, view, locale, product.category_id)


@router.message(Command("products"))
@router.message(MenuButton("menu.products"))
async def cmd_products(message: Message, session: AsyncSession, user: User) -> None:
    text, markup = await render_categories(session, user.locale)
    await message.answer(text, reply_markup=markup)


@router.callback_query(CategoryCB.filter())
async def on_category(query: CallbackQuery, callback_data: CategoryCB, session: AsyncSession, user: User) -> None:
    if not query.message:
        return
    category_id = int(callback_data.id)
    page_num = callback_data.page if callback_data.action == "page" else 1
    rendered = await render_product_list(session, category_id, page_num, user.locale)
    if rendered is None:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(ProductCB.filter())
async def on_product(query: CallbackQuery, callback_data: ProductCB, session: AsyncSession, user: User) -> None:
    if not query.message:
        return

    if callback_data.action == "buy":
        from app.bot.handlers.orders.checkout import render_payment_choice

        rendered = await render_payment_choice(session, int(callback_data.id), user)
        if rendered is None:
            await query.answer(t("common.unknown_action", user.locale), show_alert=True)
            return
        text, markup = rendered
        await query.message.edit_text(text, reply_markup=markup)
        await query.answer()
        return

    rendered = await render_product_detail(session, int(callback_data.id), user.locale)
    if rendered is None:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()
