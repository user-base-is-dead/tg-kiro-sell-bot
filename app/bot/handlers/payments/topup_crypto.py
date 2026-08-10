from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession
from app.bot.callbacks import NavCB
from app.bot.states.topup_form import TopUpForm
from app.database.models.user import User
from app.database.models.crypto import CryptoPayment
from app.locales.i18n import t
from app.services.payments.blockchain_monitor import BlockchainMonitor
from app.utils.time import as_utc

router = Router(name="payments.topup_crypto")

PAYMENT_TIMEOUT_MINUTES = 15
SERVICE_FEE = 0.2  # USD


async def render_topup_packages(locale: str, show_title: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    """Show available top-up packages."""
    if show_title:
        text = (
            "💰 <b>Top Up Wallet - Crypto</b>\n\n"
            "Current balance: $0.00\n\n"
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


async def render_payment_details(
    session: AsyncSession,
    user_id: int,
    amount_usd: float,
    locale: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Show payment details with wallet address and countdown."""
    from app.core.config import get_settings

    settings = get_settings()
    monitor = BlockchainMonitor()

    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=PAYMENT_TIMEOUT_MINUTES)

    # Calculate total with service fee
    total_amount = amount_usd + SERVICE_FEE

    payment = CryptoPayment(
        user_id=user_id,
        product_amount_minor=int(amount_usd * 100),
        expected_amount=str(total_amount),  # Store total (what they actually send)
        currency="USDT",
        status="PENDING",
        description=f"topup:{amount_usd}",
        created_at=now,
    )
    session.add(payment)
    await session.flush()

    minutes = PAYMENT_TIMEOUT_MINUTES
    text = (
        "🔗 <b>Send Payment</b>\n\n"
        f"<b>Network:</b> BNB Smart Chain (BSC)\n"
        f"<b>Token:</b> USDT (BEP-20)\n"
        f"<b>Wallet Address:</b> <code>{monitor.wallet_address}</code>\n\n"
        f"💰 <b>Top-Up Amount:</b> ${amount_usd:.2f}\n"
        f"🏷️ <b>Service Fee:</b> ${SERVICE_FEE:.2f}\n"
        f"📊 <b>Total to Send:</b> <b>${total_amount:.2f} USDT</b>\n\n"
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
        topup_amount = float(payment.expected_amount) - SERVICE_FEE
        text = (
            f"✅ <b>Payment Confirmed!</b>\n\n"
            f"Top-Up Credited: ${topup_amount:.2f} USDT\n"
            f"Service Fee: ${SERVICE_FEE:.2f}\n"
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

    text, markup = await render_topup_packages(user.locale)
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
