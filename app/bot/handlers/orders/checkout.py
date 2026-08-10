from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import NavCB, OrderCB
from app.bot.delivery_notes import delivery_note
from app.bot.keyboards.common import nav_row
from app.bot.keyboards.products import category_back_target
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.database.models.user import User
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.locales.i18n import t
from app.services import order_service, stock_hold_service
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

    # Reserve ONE credential for this buyer — not the product. The other credentials stay on the
    # shelf and other buyers can check out against them at the same time. `hold_one` is re-entrant,
    # so coming back to this screen refreshes the same credential instead of taking a second.
    held = await stock_hold_service.hold_one(session, product.id, user.id)
    if held is None:
        # Everything free was taken between rendering the payment chooser and getting here.
        return None
    remaining = await stock_hold_service.seconds_remaining(session, product.id, user.id)
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
    # One exit, not two. This screen used to carry ❌ Cancel *and* 🔙 Back, which looked like a
    # choice but wasn't: both left the screen, and neither released the 5-minute hold taken above —
    # so backing out locked the item away from every other buyer until the hold expired. Back is now
    # the single way out and it does the cancelling, exactly like the crypto invoice's exit row.
    rows = [
        [
            btn(
                t("menu.confirm", user.locale),
                OrderCB(action="confirm", product_id=str(product.id)).pack(),
                SUCCESS,
            )
        ],
        [
            btn(
                t("menu.back", user.locale),
                OrderCB(action="cancel", product_id=str(product.id)).pack(),
                DANGER,
            ),
            btn(t("menu.home", user.locale), NavCB(target="home").pack(), PRIMARY),
        ],
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
    """Crypto route: reserve the credential, then open a top-up invoice for the shortfall.

    Picking a payment method is what starts the reservation, and crypto is a payment method — so a
    credential is held here exactly as it is on the wallet route. Only that one credential is held;
    the rest of the pool stays buyable by everyone else.

    The hold still lasts five minutes while a transfer can take longer. That is deliberate: the
    money lands in the wallet either way, so a buyer whose hold lapses keeps the balance and can
    spend it on anything, including re-buying this product if a credential is free. Holding stock
    open-endedly against an unconfirmed chain transfer would take it from buyers who are ready to
    pay now.
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

    held = await stock_hold_service.hold_one(session, product.id, user.id)
    if held is None:
        await query.answer(t("errors.out_of_stock", user.locale), show_alert=True)
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
    """The confirm screen's `🔙 Back` — it hands the held credential straight back.

    Cancelling is explicit information: the buyer is not coming back, so the credential returns to
    the pool now rather than sitting out the rest of its five minutes. With one credential left,
    that is the difference between the next shopper buying immediately and being told to wait.
    """
    if not query.message:
        return

    from app.bot.handlers.products.browse import render_product_detail

    product_id = int(callback_data.product_id)
    await stock_hold_service.release(session, product_id, user.id)

    rendered = await render_product_detail(session, product_id, user.locale, user_id=user.id)
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

    # Both routes get the how-to-use-it note: an AUTO buyer needs it alongside the key they just
    # got, and a MANUAL buyer needs to know what is coming before it arrives.
    note = await delivery_note(session, placed.order_item.product_id, user.locale)
    if note:
        lines.append(note)

    await query.message.edit_text("\n\n".join(lines))
    await query.answer()
