from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import OrderCB
from app.bot.keyboards.common import confirm_row, nav_row
from app.database.models.user import User
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.locales.i18n import t
from app.services import order_service, order_hold_service
from app.services.catalog_service import compute_display_status
from app.utils.errors import UserError
from app.utils.money import format_minor

router = Router(name="orders.checkout")


async def render_checkout_confirm(session: AsyncSession, product_id: int, user: User) -> tuple[str, object] | None:
    product = await ProductRepo(session).get_by_id(product_id)
    if product is None or not product.is_active:
        return None

    view = await compute_display_status(session, product)
    if view.display_status.value not in ("IN_STOCK", "LOW_STOCK"):
        return None

    wallet = await WalletRepo(session).get_or_create(user.id, currency=product.currency)

    # Create 5-minute hold on product
    hold = await order_hold_service.create_hold(session, product.id, user.id)
    remaining = await order_hold_service.get_time_remaining(session, product.id, user.id)
    minutes = remaining // 60
    seconds = remaining % 60

    text = (
        t("orders.confirm_title", user.locale) + "\n\n" + t(
            "orders.confirm_body",
            user.locale,
            name=product.name,
            price=format_minor(product.price_minor, product.currency),
            balance=format_minor(wallet.balance_minor, wallet.currency),
        )
        + f"\n\n⏱️ <b>Payment expires in:</b> {minutes}m {seconds}s"
    )
    rows = [
        confirm_row(
            user.locale,
            OrderCB(action="confirm", product_id=str(product.id)).pack(),
            OrderCB(action="cancel", product_id=str(product.id)).pack(),
        ),
        nav_row(user.locale, back_target=f"cat-{product.category_id}"),
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(OrderCB.filter(F.action == "cancel"))
async def on_checkout_cancel(query: CallbackQuery, callback_data: OrderCB, session: AsyncSession, user: User) -> None:
    if not query.message:
        return

    from app.bot.handlers.products.browse import render_product_detail

    rendered = await render_product_detail(session, int(callback_data.product_id), user.locale)
    if rendered:
        text, markup = rendered
        await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(OrderCB.filter(F.action == "confirm"))
async def on_checkout_confirm(query: CallbackQuery, callback_data: OrderCB, session: AsyncSession, user: User) -> None:
    if not query.message:
        return

    try:
        placed = await order_service.place_order(
            session, user_id=user.id, product_id=int(callback_data.product_id)
        )
    except UserError as exc:
        await query.answer(t(exc.i18n_key, user.locale), show_alert=True)
        return

    order = placed.order
    lines = [t("orders.placed", user.locale, order_number=order.order_number)]
    if placed.delivered_payload is not None:
        warranty_days = placed.order_item.warranty_days
        lines.append(
            t("orders.auto_delivery", user.locale, payload=placed.delivered_payload, warranty_days=warranty_days)
        )
    else:
        lines.append(t("orders.manual_pending", user.locale))

    await query.message.edit_text("\n\n".join(lines))
    await query.answer()
