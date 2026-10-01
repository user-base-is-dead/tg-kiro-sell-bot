from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import NavCB, OrderCB
from app.bot.filters.menu_button import menu_labels
from app.bot.states.checkout_form import CheckoutForm
from app.bot.keyboards.common import nav_row
from app.bot.keyboards.products import category_back_target
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.database.models.catalog import FulfillmentMode
from app.database.models.user import User
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.support_repo import SupportRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.locales.i18n import _load, t
from app.services import order_service, purchase_service, stock_hold_service
from app.services.catalog_service import compute_display_status
from app.utils.errors import UserError
from app.utils.money import format_minor
from app.utils.text import PAD

router = Router(name="orders.checkout")


MAX_MADE_TO_ORDER_QTY = 99

# Characters that can only be someone trying to answer "how many?", however badly. A message
# containing anything else was aimed at a person, not at this prompt.
_QUANTITY_CHARS = set("0123456789 .,-+")


async def buyable_quantity(session: AsyncSession, product) -> int:
    """The most units this buyer could be sold right now.

    For anything with a pool or a hand-set count that is the count itself — the shelf is the cap,
    so an order can never be taken for stock that does not exist. A made-to-order product has no
    number to cap against, so it gets a ceiling that exists only to stop a typo becoming a
    thousand-unit order.
    """
    if product.fulfillment_mode is FulfillmentMode.MANUAL and product.manual_stock is None:
        return MAX_MADE_TO_ORDER_QTY
    view = await compute_display_status(session, product)
    return max(0, view.available_stock)


async def render_quantity_prompt(
    session: AsyncSession, product_id: int, user: User
) -> tuple[str, object, int] | None:
    """How many, before how to pay. Returns the screen plus the cap, for the caller to stash.

    Typed rather than picked from buttons: stock runs to whatever the admin uploaded, and a
    keyboard cannot offer 37 without becoming a wall of digits.
    """
    product = await ProductRepo(session).get_by_id(product_id)
    if product is None or not product.is_active:
        return None

    view = await compute_display_status(session, product)
    if view.display_status.value not in ("IN_STOCK", "LOW_STOCK"):
        return None

    cap = await buyable_quantity(session, product)
    if cap < 1:
        return None

    price = format_minor(product.price_minor, product.currency)
    lines = [
        "🔢 <b>Choose quantity</b>",
        "",
        f"{product.name}",
        f"💰 {price} per item",
        "",
        "Type how many you want as a whole number, like <code>1</code>, <code>2</code> or "
        "<code>3</code>. No decimals, no words.",
        "",
        f"📦 Available right now: <b>{cap}</b>",
    ]
    rows = [
        [
            btn(
                "1️⃣ Buy 1",
                OrderCB(action="pay", product_id=str(product.id), qty=1).pack(),
                SUCCESS,
            )
        ],
        nav_row(user.locale, back_target=category_back_target(product.category_id)),
    ]
    return "\n".join(lines) + f"\n{PAD}", InlineKeyboardMarkup(inline_keyboard=rows), cap


async def render_payment_choice(
    session: AsyncSession, product_id: int, user: User, qty: int = 1
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

    # Re-checked against the shelf on every render, not trusted from the callback: the number was
    # typed a screen ago and other buyers have been shopping since.
    qty = max(1, min(int(qty), await buyable_quantity(session, product)))

    wallet = await WalletRepo(session).get_or_create(user.id, currency=product.currency)
    total_minor = product.price_minor * qty
    shortfall_minor = max(0, total_minor - wallet.balance_minor)
    covered = shortfall_minor == 0

    price = format_minor(product.price_minor, product.currency)
    total = format_minor(total_minor, product.currency)
    balance = format_minor(wallet.balance_minor, wallet.currency)
    lines = [
        "💳 <b>How would you like to pay?</b>",
        "",
        f"{product.name}",
    ]
    # A quantity of one is the ordinary case and does not need arithmetic spelled out at it.
    if qty > 1:
        lines.append(f"🔢 Quantity: <b>{qty}</b> × {price}")
    lines += [
        f"<blockquote>💰 Total: <b>{total}</b>\n👛 Wallet balance: {balance}</blockquote>",
        "",
    ]
    if covered:
        lines.append(
            "Your balance covers this. Pay from your wallet, or pay the full amount in crypto and "
            "keep your balance as it is."
        )
    else:
        short = format_minor(shortfall_minor, product.currency)
        lines.append(f"⚠️ You're {short} short. Pay with crypto to cover the difference.")

    rows = [
        [
            btn(
                "👛 Pay from Wallet" if covered else f"👛 Wallet ({balance})",
                OrderCB(action="wallet", product_id=str(product.id), qty=qty).pack(),
                SUCCESS if covered else NEUTRAL,
            )
        ],
        [
            btn(
                "💎 Pay with USDT",
                OrderCB(action="crypto", product_id=str(product.id), qty=qty).pack(),
                PRIMARY,
            )
        ],
        nav_row(user.locale, back_target=category_back_target(product.category_id)),
    ]
    return "\n".join(lines) + f"\n{PAD}", InlineKeyboardMarkup(inline_keyboard=rows)


async def render_checkout_confirm(
    session: AsyncSession, product_id: int, user: User, qty: int = 1
) -> tuple[str, object] | None:
    product = await ProductRepo(session).get_by_id(product_id)
    if product is None or not product.is_active:
        return None

    view = await compute_display_status(session, product)
    if view.display_status.value not in ("IN_STOCK", "LOW_STOCK"):
        return None

    qty = max(1, int(qty))
    wallet = await WalletRepo(session).get_or_create(user.id, currency=product.currency)

    # Reserve exactly as many credentials as this buyer asked for — never the product. Everything
    # they did not ask for stays on the shelf and other buyers can check out against it at the same
    # time. `hold_many` is re-entrant, so coming back to this screen refreshes the same credentials
    # instead of taking a second set, and lowering the number hands the surplus straight back.
    #
    # MANUAL products are exempt. They are not backed by a code pool at all — the admin fulfils each
    # order by hand — so there is nothing legitimate to reserve. Holding here did real damage twice
    # over: a manual product with an unrelated stock pool had a credential quietly taken off the
    # shelf on every checkout, and a manual product with an EMPTY pool could not be bought at all,
    # because `hold_one` returned None and this screen refused to render.
    if product.fulfillment_mode is FulfillmentMode.MANUAL:
        remaining = 0
    else:
        held = await stock_hold_service.hold_many(session, product.id, user.id, qty)
        if held < 1:
            # Everything free was taken between rendering the payment chooser and getting here.
            return None
        # Short is not empty. Someone else took part of what this buyer asked for while they were
        # choosing how to pay, and confirming for the number they can actually have beats a dead
        # end that makes them start over — the price on the screen moves with it.
        qty = min(qty, held)
        remaining = await stock_hold_service.seconds_remaining(session, product.id, user.id)
    # No hold, no countdown: a "payment expires in 0m 0s" line on a manual product would be a
    # deadline the buyer cannot miss and does not have.
    countdown = f"\n\n⏱️ <b>Reserved for you:</b> {remaining // 60}m {remaining % 60}s to pay" if remaining else ""

    name = product.name if qty == 1 else f"{product.name}  ×{qty}"
    text = (
        t("orders.confirm_title", user.locale) + "\n\n" + t(
            "orders.confirm_body",
            user.locale,
            name=name,
            price=format_minor(product.price_minor * qty, product.currency),
            balance=format_minor(wallet.balance_minor, wallet.currency),
        )
        + countdown
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
                OrderCB(action="confirm", product_id=str(product.id), qty=qty).pack(),
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


class _NotANavigationPress(Filter):
    """True for anything that is an attempt at a quantity, false for the ways out.

    The quantity question waits on a plain message, and the reply keyboard is made of plain
    messages too. Without this, a shopper who changed their mind and pressed 📦 Orders got
    "send a whole number" instead of their orders — the question would have held them hostage
    until they typed a number they no longer wanted. Commands are let through for the same reason.
    """

    async def __call__(self, message: Message, **data) -> bool:
        text = (message.text or "").strip()
        if text.startswith("/"):
            return False
        locale = user.locale if (user := data.get("user")) else "en"
        # Retired labels too: a client still showing the old panel sends those, and they are just as
        # much a way out as today's.
        return text not in {
            label for key in _load(locale).get("menu", {}) for label in menu_labels(f"menu.{key}", locale)
        }


@router.message(CheckoutForm.quantity, _NotANavigationPress())
async def on_quantity_typed(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    """The typed number. Whole numbers only, and never more than the shelf holds.

    `isdigit()` rather than parsing: it rejects "1.5", "-2", "1 2" and "two" in one go, and each of
    those is a different way of asking for something that cannot be delivered. A decimal in
    particular has to be refused rather than rounded — nobody agrees on which way 1.5 rounds, and
    guessing spends the buyer's money on the answer.
    """
    data = await state.get_data()
    product_id = data.get("product_id")
    if product_id is None:
        await state.clear()
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        # Prose is not a failed attempt at a number, it is a message aimed somewhere else. This
        # question has no deadline and nothing closes it, so a buyer who pressed Buy Now, wandered
        # off, and came back hours later to answer support found their words eaten and "send a
        # whole number" sent back — support never heard from them, and the reason was invisible
        # from both ends. Anything made only of digits and separators is still treated as a typo
        # here ("1.5", "2 3"), because that really was someone answering this question.
        if raw and not set(raw) <= _QUANTITY_CHARS:
            from app.bot.handlers.support.relay import dm_relay

            if await SupportRepo(session).get_open_for_user(user.id) is not None:
                await state.clear()
                await dm_relay(message, session, user)
                return
        await message.answer(
            "Please send a whole number, like <code>1</code>, <code>2</code> or <code>3</code>."
        )
        return

    qty = int(raw)
    if qty < 1:
        await message.answer("The minimum is <code>1</code>.")
        return

    # The cap is re-read from the shelf, not taken from what the prompt said: minutes may have
    # passed and other buyers have been shopping.
    product = await ProductRepo(session).get_by_id(product_id)
    if product is None or not product.is_active:
        await state.clear()
        await message.answer(t("common.unknown_action", user.locale))
        return

    cap = await buyable_quantity(session, product)
    if cap < 1:
        await state.clear()
        await message.answer(t("errors.out_of_stock", user.locale))
        return
    if qty > cap:
        await message.answer(f"Only <b>{cap}</b> in stock right now — send <code>{cap}</code> or less.")
        return

    await state.clear()
    rendered = await render_payment_choice(session, product_id, user, qty)
    if rendered is None:
        await message.answer(t("errors.out_of_stock", user.locale))
        return
    text, markup = rendered
    await message.answer(text, reply_markup=markup)


@router.callback_query(OrderCB.filter(F.action == "pay"))
async def on_quantity_button(
    query: CallbackQuery, callback_data: OrderCB, state: FSMContext, session: AsyncSession, user: User
) -> None:
    """The "Just one" shortcut on the quantity screen — the same path a typed 1 takes."""
    if not query.message:
        return

    await state.clear()
    rendered = await render_payment_choice(
        session, int(callback_data.product_id), user, callback_data.qty
    )
    if rendered is None:
        await query.answer(t("errors.out_of_stock", user.locale), show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(OrderCB.filter(F.action == "wallet"))
async def on_pay_from_wallet(query: CallbackQuery, callback_data: OrderCB, session: AsyncSession, user: User) -> None:
    """Wallet route: straight to the existing confirm screen, which is where the hold is taken."""
    if not query.message:
        return

    rendered = await render_checkout_confirm(session, int(callback_data.product_id), user, callback_data.qty)
    if rendered is None:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(OrderCB.filter(F.action == "crypto"))
async def on_pay_with_crypto(query: CallbackQuery, callback_data: OrderCB, session: AsyncSession, user: User) -> None:
    """Crypto route: put the credentials in checkout, then open a 5-minute USDT invoice.

    Picking a payment method is what starts the reservation, and crypto is a payment method — so the
    credentials are HELD here exactly as on the wallet route: off the shelf, in checkout, not sold.
    Only the ones being bought are held; the rest of the pool stays buyable by everyone else.

    The invoice lives exactly as long as the hold (payments/invoices.PAYMENT_WINDOW_MINUTES). Paid in
    time, the checker job places the order itself and the buyer gets the goods in this chat. Not paid
    in time, or abandoned (✖️ Cancel, or any other button or command — see
    middlewares/pending_payment.py), and the credentials go straight back in stock. A transfer that
    still arrives afterwards is credited to the wallet, never lost.
    """
    if not query.message:
        return

    from app.bot.handlers.payments.topup_crypto import render_payment_details

    product = await ProductRepo(session).get_by_id(int(callback_data.product_id))
    if product is None or not product.is_active:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return

    qty = max(1, min(callback_data.qty, await buyable_quantity(session, product)))
    wallet = await WalletRepo(session).get_or_create(user.id, currency=product.currency)
    # A covered wallet is not a reason to refuse crypto. Plenty of buyers keep a balance on purpose
    # — saved for a bigger purchase, or just topped up for later — and would rather pay this one on
    # chain than eat into it. So when the wallet already covers the price we invoice the FULL price
    # instead of a $0.00 dead end: the top-up lands in the wallet, the purchase spends the same
    # amount back out, and the balance they were protecting ends up exactly where it started.
    total_minor = product.price_minor * qty
    shortfall_minor = max(0, total_minor - wallet.balance_minor)
    invoice_minor = shortfall_minor or total_minor

    # MANUAL products hold nothing — see `render_checkout_confirm` for why reserving a credential
    # for a hand-fulfilled order is wrong in both directions.
    if product.fulfillment_mode is not FulfillmentMode.MANUAL:
        held = await stock_hold_service.hold_many(session, product.id, user.id, qty)
        if held < 1:
            await query.answer(t("errors.out_of_stock", user.locale), show_alert=True)
            return

    # The invoice is told what it is paying for, and how many: when the transfer arrives the checker
    # places this exact order itself, so the buyer never has to come back and press Buy Now again.
    text, markup = await render_payment_details(
        session, user.id, invoice_minor / 100, user.locale, purchase_product_id=product.id, qty=qty
    )
    await query.message.edit_text(text, reply_markup=markup)
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

    # Read before anything can roll back: a rollback expires every loaded row, and touching `user`
    # afterwards would try to lazy-load it outside the async context.
    locale = user.locale
    try:
        placed = await order_service.place_order(
            session,
            user_id=user.id,
            product_id=int(callback_data.product_id),
            qty=callback_data.qty,
        )
    except UserError as exc:
        # place_order had already written the order and moved the buyer's credentials to RESERVED
        # when the wallet turned out to be short. Swallowing the error used to let the session commit
        # all of that anyway: a PENDING order nobody paid for, holding a login in "being sold" for
        # ever. The purchase is all or nothing, so everything it wrote goes — the held credentials
        # stay held for the rest of the buyer's five minutes, then return to stock.
        await session.rollback()
        await query.answer(t(exc.i18n_key, locale), show_alert=True)
        return

    # Built before the commit (it only reads) but sent after it, because this message *is* the goods:
    # for an AUTO product the text contains the decrypted login.
    text = await purchase_service.buyer_message(session, placed, locale)

    # The sale is made durable before the credentials leave the building. It used to be the other way
    # round — the payload was shown while the transaction was still open and DbSessionMiddleware
    # committed afterwards — so anything that failed at commit time (a deadlock, a dropped
    # connection, a unique-constraint conflict from a concurrent double-tap) rolled back the order
    # and the debit *after* the buyer had read the login, and handed the stock item back to the shelf
    # for somebody else to buy. Free product, and the same credential delivered twice.
    await session.commit()

    # Best-effort now that the money and the order are final, and a failed edit must not swallow the
    # payload: this screen can be the message a broadcast attached the button to, and `edit_text` is
    # rejected outright on media — same reason `products.browse._show` exists.
    try:
        await query.message.edit_text(text)
    except TelegramBadRequest:
        await query.message.answer(text)

    # After the commit as well. These are side effects — the orders-group topic, the manual-fulfilment
    # prompt, the sold-out announcement — and `order_thread_service.sync` is self-healing: whatever it
    # fails to post goes out on the next action. The thread bookkeeping it writes is committed by
    # DbSessionMiddleware when this handler returns.
    await purchase_service.after_purchase(query.bot, session, placed)
    await query.answer()
