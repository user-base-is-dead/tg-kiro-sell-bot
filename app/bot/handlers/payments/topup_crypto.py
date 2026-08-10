from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.bot.callbacks import NavCB
from app.bot.states.topup_form import TopUpForm
from app.database.models.user import User
from app.database.models.crypto import CryptoPayment
from app.locales.i18n import t
from app.services import stock_hold_service
from app.services.payments.blockchain_monitor import BlockchainMonitor
from app.utils.time import as_utc

router = Router(name="payments.topup_crypto")

PAYMENT_TIMEOUT_MINUTES = 15
SERVICE_FEE = 0.2  # USD — the base fee; see `_unique_total` for why an invoice may add cents.


async def _balance_minor(session: AsyncSession, user_id: int) -> int:
    from app.database.repositories.wallet_repo import WalletRepo

    wallet = await WalletRepo(session).get_or_create(user_id, currency="USD")
    return wallet.balance_minor


async def render_topup_packages(
    locale: str, show_title: bool = True, *, balance_minor: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    """Show available top-up packages.

    `balance_minor` is passed in rather than looked up here because the screen is rendered from
    places that already hold the wallet. It used to be hardcoded to $0.00, which told every user
    with money that they had none.
    """
    if show_title:
        text = (
            "💰 <b>Top Up Wallet - Crypto</b>\n\n"
            f"Current balance: ${balance_minor / 100:.2f}\n\n"
            "Enter the amount to top up your wallet with USDT (BNB Chain):\n"
        )
    else:
        text = "Enter the amount to top up your wallet with USDT (BNB Chain):\n"

    # Only show custom amount button
    from app.bot.callbacks import NavCB
    from app.bot.keyboards.styles import DANGER, SUCCESS, btn

    rows = [
        [btn("✏️ Enter Custom Amount", "topup_crypto_custom", SUCCESS)],
        [btn("◄ Back", NavCB(target="home").pack(), DANGER)],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _unique_total(session: AsyncSession, amount_usd: float, now: datetime) -> float:
    """The exact USDT total this invoice must receive, distinct from every other live invoice.

    A transfer is matched to an invoice by amount and nothing else — the chain carries no order id.
    So two buyers topping up $15.00 at the same time used to be handed the identical total, and the
    checker refuses to guess between them: it logs "ambiguous" and skips the transfer, leaving both
    buyers paid and neither credited. Nudging the fee up by a cent per collision makes each live
    total unique, so every transfer resolves to exactly one invoice.

    Only currently-matchable invoices reserve a total. A cancelled, confirmed or timed-out one is
    no longer a candidate, so its amount is free again and the fee never drifts upward over time.
    """
    cutoff = now - timedelta(minutes=PAYMENT_TIMEOUT_MINUTES)
    result = await session.execute(
        select(CryptoPayment.expected_amount).where(
            CryptoPayment.status == "PENDING", CryptoPayment.created_at >= cutoff
        )
    )
    taken = {round(float(a), 2) for a in result.scalars()}

    total = round(amount_usd + SERVICE_FEE, 2)
    while total in taken:
        total = round(total + 0.01, 2)
    return total


def invoice_product_id(payment: CryptoPayment) -> int | None:
    """The product this invoice was opened to buy, or None for a plain wallet top-up.

    Stored in `description` as `buy:<product_id>:<amount>`; a bare top-up keeps the old
    `topup:<amount>` form. The column is free text and nothing else reads it, so this needs no
    migration — and an unparseable value degrades to "this was a top-up", which is the safe answer.
    """
    parts = (payment.description or "").split(":")
    if len(parts) >= 2 and parts[0] == "buy":
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


async def render_payment_details(
    session: AsyncSession,
    user_id: int,
    amount_usd: float,
    locale: str,
    *,
    purchase_product_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Show payment details with wallet address and countdown.

    `purchase_product_id` marks an invoice that was opened from a product's checkout rather than
    from Top Up Wallet. Mechanically the two are identical — both credit the wallet — but they are
    not the same thing to the person reading the screen, so the invoice labels itself accordingly
    and its Cancel returns to the product instead of to the top-up menu.
    """
    monitor = BlockchainMonitor()
    now = datetime.now(UTC)

    total_amount = await _unique_total(session, amount_usd, now)
    fee = round(total_amount - amount_usd, 2)

    buying = purchase_product_id is not None
    payment = CryptoPayment(
        user_id=user_id,
        product_amount_minor=int(amount_usd * 100),
        expected_amount=str(total_amount),  # Store total (what they actually send)
        currency="USDT",
        status="PENDING",
        description=f"buy:{purchase_product_id}:{amount_usd}" if buying else f"topup:{amount_usd}",
        created_at=now,
    )
    session.add(payment)
    await session.flush()

    minutes = PAYMENT_TIMEOUT_MINUTES
    heading = "🔗 <b>Pay for your order</b>" if buying else "🔗 <b>Send Payment</b>"
    amount_label = "Order Amount" if buying else "Top-Up Amount"
    text = (
        f"{heading}\n\n"
        f"<b>Network:</b> BNB Smart Chain (BSC)\n"
        f"<b>Token:</b> USDT (BEP-20)\n"
        f"<b>Wallet Address:</b> <code>{monitor.wallet_address}</code>\n\n"
        f"💰 <b>{amount_label}:</b> ${amount_usd:.2f}\n"
        f"🏷️ <b>Service Fee:</b> ${fee:.2f}\n"
        f"📊 <b>Total to Send:</b>\n"
        f"👉 <code>{total_amount:.2f}</code> <b>USDT</b>\n\n"
        f"📋 <b>Tap the amount above to copy it</b>, then paste it into the amount field of your "
        f"wallet. Do not type it by hand and do not round it — the exact figure "
        f"<code>{total_amount:.2f}</code> is how your payment is matched to "
        f"{'this order' if buying else 'your account'}.\n\n"
        f"⏱️ <b>Payment expires in:</b> {minutes} minutes\n\n"
        "✅ Payment will be auto-confirmed when received.\n"
        "⚠️ Send exactly the total amount shown to ensure auto-confirmation."
    )

    from app.bot.keyboards.styles import SUCCESS, btn

    rows = [
        [btn("✓ Check Payment Status", f"check_topup_crypto:{payment.id}", SUCCESS)],
        _exit_row(locale, payment.id),
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _exit_row(locale: str, payment_id: int) -> list[InlineKeyboardButton]:
    """Exit row for a live payment. Cancel replaces the plain Back button: leaving this screen
    while the invoice is still PENDING would otherwise abandon a record that the checker job keeps
    matching incoming transfers against for the next 15 minutes. Cancel closes the invoice *and*
    goes one step back, so it does everything Back did and one thing more."""
    from app.bot.keyboards.styles import DANGER, PRIMARY, btn

    return [
        btn(t("menu.cancel", locale), f"cancel_topup_crypto:{payment_id}", DANGER),
        btn(t("menu.home", locale), NavCB(target="home").pack(), PRIMARY),
    ]


@router.callback_query(F.data.startswith("check_topup_crypto:"))
async def on_check_topup_payment(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Check payment status."""
    if not query.message:
        return

    payment_id = int(query.data.split(":")[-1])
    payment = await session.get(CryptoPayment, payment_id)

    if payment is None or payment.user_id != user.id:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return

    if payment.status == "PENDING":
        expires_at = as_utc(payment.created_at) + timedelta(minutes=PAYMENT_TIMEOUT_MINUTES)
        remaining = (expires_at - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            # Nothing has changed yet, so the screen shouldn't change either — replacing it would
            # take away the wallet address and amount the user is still in the middle of paying.
            # A popup reports "not in yet" and leaves the invoice on screen to keep copying from.
            await query.answer(
                t(
                    "topup.not_received",
                    user.locale,
                    minutes=int(remaining) // 60,
                    seconds=int(remaining) % 60,
                ),
                show_alert=True,
            )
            return

        payment.status = "EXPIRED"
        await session.flush()
        text = "❌ <b>Payment Expired</b>\n\nThe payment window has closed. Please start a new top-up."
    elif payment.status == "CONFIRMED":
        # Derived from the invoice, not recomputed from the base fee: the fee on this specific
        # invoice may carry disambiguating cents, and `product_amount_minor` is the authoritative
        # record of what the wallet was actually credited with.
        topup_amount = payment.product_amount_minor / 100
        fee = round(float(payment.expected_amount) - topup_amount, 2)
        text = (
            f"✅ <b>Payment Confirmed!</b>\n\n"
            f"Top-Up Credited: ${topup_amount:.2f} USDT\n"
            f"Service Fee: ${fee:.2f}\n"
            f"Transaction: <code>{payment.tx_hash}</code>\n\n"
            "Your wallet has been credited. Thank you!"
        )
    elif payment.status == "MISMATCH":
        text = (
            f"⚠️ <b>Amount Mismatch</b>\n\n"
            f"Expected: ${payment.expected_amount} USDT\n"
            f"Received: ${payment.actual_amount} USDT\n\n"
            "Please contact support."
        )
    else:
        text = f"❓ <b>Payment Status: {payment.status}</b>"

    from app.bot.keyboards.styles import DANGER, btn

    # Only settled payments reach here — a still-live one returns above with a popup. So there is
    # never anything left to cancel, and plain Back is the honest button.
    rows = [[btn(t("menu.back", user.locale), NavCB(target="topup").pack(), DANGER)]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()


@router.callback_query(F.data.startswith("cancel_topup_crypto:"))
async def on_cancel_topup_payment(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Close a pending invoice and step back to the top-up screen."""
    if not query.message:
        return

    payment_id = int(query.data.split(":")[-1])
    payment = await session.get(CryptoPayment, payment_id)

    # Ownership is re-derived from the DB, never trusted from the callback payload — the id in it
    # is just a routing hint anyone could replay.
    if payment is None or payment.user_id != user.id:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return

    # Only a live invoice changes state. A payment that already confirmed while the user was
    # looking at the screen must not be cancelled out from under them.
    if payment.status == "PENDING":
        payment.status = "CANCELLED"
        await session.flush()

    # Cancel means "one step back", and where back *is* depends on where the invoice came from. An
    # invoice opened from a product's checkout returns to that product — dropping a mid-purchase
    # buyer onto the Top Up Wallet menu stranded them somewhere they never asked to be, with no
    # trace of what they had been buying.
    product_id = invoice_product_id(payment)
    if product_id is not None:
        from app.bot.handlers.products.browse import render_product_detail

        await stock_hold_service.release(session, product_id, user.id)
        rendered = await render_product_detail(session, product_id, user.locale, user_id=user.id)
        if rendered is not None:
            text, markup = rendered
            await query.message.edit_text(text, reply_markup=markup)
            await query.answer(t("topup.cancelled", user.locale))
            return

    text, markup = await render_topup_packages(user.locale, balance_minor=await _balance_minor(session, user.id))
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer(t("topup.cancelled", user.locale))


@router.callback_query(F.data == "topup_crypto_custom")
async def on_topup_custom(query: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    """Handle custom amount button - ask user to input amount."""
    from app.bot.callbacks import NavCB
    from app.bot.keyboards.styles import DANGER, btn

    if not query.message:
        return

    await state.set_state(TopUpForm.amount)
    # Back here means "one step back to the top-up screen", not all the way home.
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[btn(t("menu.back", user.locale), NavCB(target="topup").pack(), DANGER)]]
    )
    await query.message.edit_text(
        "💰 <b>Enter Custom Amount</b>\n\n"
        "Type the amount in USD (e.g., 15.50):\n\n"
        "Min: $1.00 | Max: $10000.00",
        reply_markup=markup,
    )
    await query.answer()


@router.message(TopUpForm.amount, F.text)
async def process_custom_topup(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    """Process custom top-up amount."""

    if not message.text:
        return

    text = message.text.strip()

    # Try to parse as float
    try:
        amount = float(text)
    except ValueError:
        await message.answer("❌ Invalid amount. Please enter a valid number (e.g., 15.50)")
        return

    # Validate amount range
    if amount < 1.0:
        await message.answer("❌ Minimum top-up is $1.00")
        return

    if amount > 10000.0:
        await message.answer("❌ Maximum top-up is $10000.00")
        return

    # Show payment details for custom amount
    await state.clear()
    text_msg, markup = await render_payment_details(session, user.id, amount, user.locale)
    await message.answer(text_msg, reply_markup=markup)
