from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession
from app.bot.keyboards.common import nav_row
from app.bot.states.topup_form import TopUpForm
from app.database.models.user import User
from app.database.models.crypto import CryptoPayment
from app.locales.i18n import t
from app.services.payments.blockchain_monitor import BlockchainMonitor
from app.utils.errors import UserError

router = Router(name="payments.topup_crypto")

# Top-up packages in USD
TOPUP_PACKAGES = {
    "10": {"usd": 10.0, "label": "$10"},
    "25": {"usd": 25.0, "label": "$25"},
    "50": {"usd": 50.0, "label": "$50"},
    "100": {"usd": 100.0, "label": "$100"},
}

PAYMENT_TIMEOUT_MINUTES = 15
SERVICE_FEE = 0.2  # USD


async def render_topup_packages(locale: str, show_title: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    """Show available top-up packages."""
    if show_title:
        text = (
            "💰 <b>Top Up Wallet - Crypto</b>\n\n"
            "Current balance: $0.00\n\n"
            "Choose an amount to top up your wallet with USDT (BNB Chain):\n"
        )
    else:
        text = "Choose an amount to top up your wallet with USDT (BNB Chain):\n"
    rows = [
        [
            InlineKeyboardButton(
                text=pkg["label"],
                callback_data=f"topup_crypto:{pkg_id}",
            )
            for pkg_id, pkg in list(TOPUP_PACKAGES.items())[i : i + 2]
        ]
        for i in range(0, len(TOPUP_PACKAGES), 2)
    ]
    # Add custom amount button
    rows.append([
        InlineKeyboardButton(
            text="✏️ Custom Amount",
            callback_data="topup_crypto_custom",
        )
    ])
    # Add back button
    from app.bot.callbacks import NavCB
    rows.append([
        InlineKeyboardButton(
            text="◄ Back",
            callback_data=NavCB(target="welcome").pack(),
        )
    ])
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

    rows = [
        [
            InlineKeyboardButton(
                text="✓ Check Payment Status",
                callback_data=f"check_topup_crypto:{payment.id}",
            )
        ],
        nav_row(locale, back_target="topup_packages"),
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "topup_packages")
async def on_topup_packages(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Show top-up package selection."""
    if not query.message:
        return
    text, markup = await render_topup_packages(user.locale)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(F.data.startswith("topup_crypto:"))
async def on_topup_crypto_select(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Handle package selection."""
    if not query.message:
        return

    pkg_id = query.data.split(":")[-1]
    if pkg_id not in TOPUP_PACKAGES:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return

    pkg = TOPUP_PACKAGES[pkg_id]
    text, markup = await render_payment_details(session, user.id, pkg["usd"], user.locale)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(F.data.startswith("check_topup_crypto:"))
async def on_check_topup_payment(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Check payment status."""
    if not query.message:
        return

    payment_id = int(query.data.split(":")[-1])
    payment = await session.get(__import__("app.database.models.crypto", fromlist=["CryptoPayment"]).CryptoPayment, payment_id)

    if payment is None or payment.user_id != user.id:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return

    if payment.status == "PENDING":
        remaining = (payment.created_at + timedelta(minutes=PAYMENT_TIMEOUT_MINUTES) - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            minutes = int(remaining) // 60
            seconds = int(remaining) % 60
            topup_amount = float(payment.expected_amount) - SERVICE_FEE
            text = (
                f"⏳ <b>Payment Status: Pending</b>\n\n"
                f"Top-Up: ${topup_amount:.2f}\n"
                f"Service Fee: ${SERVICE_FEE:.2f}\n"
                f"Total to Send: ${payment.expected_amount} USDT\n\n"
                f"⏱️ Time remaining: {minutes}m {seconds}s\n\n"
                "Waiting for blockchain confirmation..."
            )
        else:
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

    from app.bot.callbacks import NavCB
    rows = [[
        InlineKeyboardButton(
            text="◄ Back",
            callback_data=NavCB(target="topup").pack(),
        )
    ]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()


@router.callback_query(F.data == "topup_crypto_custom")
async def on_topup_custom(query: CallbackQuery, state: FSMContext, session: AsyncSession, user: User) -> None:
    """Handle custom amount button - ask user to input amount."""
    from app.bot.states.topup_form import TopUpForm
    from app.bot.keyboards.common import back_keyboard

    if not query.message:
        return

    await state.set_state(TopUpForm.amount)
    await query.message.edit_text(
        "💰 <b>Enter Custom Amount</b>\n\n"
        "Type the amount in USD (e.g., 15.50):\n\n"
        "Min: $1.00 | Max: $10000.00",
        reply_markup=back_keyboard(user.locale),
    )
    await query.answer()


@router.message(TopUpForm.amount, F.text)
async def process_custom_topup(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    """Process custom top-up amount."""
    from app.bot.states.topup_form import TopUpForm

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
