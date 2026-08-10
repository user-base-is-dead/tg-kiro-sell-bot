from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import OrderCB
from app.bot.keyboards.common import confirm_row, nav_row
from app.bot.keyboards.products import category_back_target
from app.bot.keyboards.styles import NEUTRAL, PRIMARY, SUCCESS, btn
from app.database.models.user import User
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.locales.i18n import t
from app.services import order_service, order_hold_service
from app.services.catalog_service import compute_display_status
from app.utils.errors import UserError
from app.utils.money import format_minor
from app.utils.text import PAD

router = Router(name="orders.checkout")


async def render_payment_choice(
    session: AsyncSession, product_id: int, user: User
) -> tuple[str, object] | None:
    """How the buyer wants to pay, before anything is held or debited.

    Crypto does not pay for the order directly. It tops the wallet up and the wallet buys, so
    place_order stays the only thing that creates an order and the crypto checker job stays a
    pure wallet-credit path. The shortfall is what gets pre-filled into the top-up.
    """
    product = await ProductRepo(session).get_by_id(product_id)
    if product is None or not product.is_active:
        return None

    view = await compute_display_status(session, product)
    if view.display_status.value not in ("IN_STOCK", "LOW_STOCK"):
        return None

    wallet = await WalletRepo(session).get_or_create(user.id, currency=product.currency)
    shortfall_minor = max(0, product.price_minor - wallet.balance_minor)
    covered = shortfall_minor == 0

    price = format_minor(product.price_minor, product.currency)
    balance = format_minor(wallet.balance_minor, wallet.currency)
    lines = [
        "🛒 <b>Choose Payment Method</b>",
        "",
        f"{product.name}",
        f"💰 Price: {price}",
        f"💳 Wallet balance: {balance}",
        "",
    ]
    if covered:
        lines.append("Your wallet covers this. Pay from it, or top up with crypto first.")
    else:
        short = format_minor(shortfall_minor, product.currency)
        lines.append(f"⚠️ You are {short} short. Top up with crypto to cover it.")

    rows = [
        [
            btn(
                "💳 Pay from Wallet" if covered else f"💳 Wallet ({balance})",
                OrderCB(action="wallet", product_id=str(product.id)).pack(),
                SUCCESS if covered else NEUTRAL,
            )
        ],
        [
            btn(
                "💎 Pay with Crypto (USDT)",
                OrderCB(action="crypto", product_id=str(product.id)).pack(),
                PRIMARY,
            )
        ],
        nav_row(user.locale, back_target=category_back_target(product.category_id)),
    ]
    return "\n".join(lines) + f"\n{PAD}", InlineKeyboardMarkup(inline_keyboard=rows)


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
        + f"\n{PAD}"
    )
    rows = [
        confirm_row(
            user.locale,
            OrderCB(action="confirm", product_id=str(product.id)).pack(),
            OrderCB(action="cancel", product_id=str(product.id)).pack(),
        ),
        nav_row(user.locale, back_target=category_back_target(product.category_id)),
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(OrderCB.filter(F.action == "wallet"))
async def on_pay_from_wallet(query: CallbackQuery, callback_data: OrderCB, session: AsyncSession, user: User) -> None:
    """Wallet route: straight to the existing confirm screen, which is where the hold is taken."""
    if not query.message:
        return

    rendered = await render_checkout_confirm(session, int(callback_data.product_id), user)
    if rendered is None:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(OrderCB.filter(F.action == "crypto"))
async def on_pay_with_crypto(query: CallbackQuery, callback_data: OrderCB, session: AsyncSession, user: User) -> None:
    """Crypto route: open a top-up invoice for the shortfall.

    No hold is taken here. A crypto transfer can take minutes and the hold is five, so holding
    stock across it would expire mid-payment and fail the purchase after the buyer had already
    sent funds. The money lands in the wallet either way, so nothing is lost if the item sells
    out first — the buyer keeps the balance and can spend it on anything.
    """
    if not query.message:
        return

    from app.bot.handlers.payments.topup_crypto import render_payment_details

    product = await ProductRepo(session).get_by_id(int(callback_data.product_id))
    if product is None or not product.is_active:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return

    wallet = await WalletRepo(session).get_or_create(user.id, currency=product.currency)
    shortfall_minor = max(0, product.price_minor - wallet.balance_minor)
    if shortfall_minor == 0:
        # Already covered — sending them to an invoice for $0.00 would be a dead end.
        await query.answer(t("orders.wallet_already_covers", user.locale), show_alert=True)
        return

    text, markup = await render_payment_details(
        session, user.id, shortfall_minor / 100, user.locale
    )
    await query.message.edit_text(
        text + "\n\n🛒 <b>After this confirms, press Buy Now again to complete the purchase.</b>",
        reply_markup=markup,
    )
    await query.answer()


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
