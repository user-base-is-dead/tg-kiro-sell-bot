from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import NavCB, ProductCB
from app.database.models.crypto import CryptoPayment
from app.database.models.order import Order
from app.database.models.user import User
from app.locales.i18n import t
from app.services import stock_hold_service
from app.services.payments import invoices
from app.services.payments.blockchain_monitor import MATCH_TOLERANCE, BlockchainMonitor
from app.services.payments.invoices import invoice_product_id  # noqa: F401 - re-exported for callers

router = Router(name="payments.topup_crypto")

# The USDT invoice a buyer pays at checkout, and the two buttons that act on it (check / cancel).
# Topping a wallet up ahead of time — the ➕ Add Funds screen and its "enter an amount" form — was
# removed; an invoice is only ever opened for a purchase now. Mechanically it is still a wallet
# credit that the purchase then spends, which is why the names still say "topup". The rules for how
# long it lives and how it ends are in app/services/payments/invoices.py.

PAYMENT_TIMEOUT_MINUTES = invoices.PAYMENT_WINDOW_MINUTES
SERVICE_FEE = 0.2  # USD — flat, per order, never varies. See `_unique_total`.

# How many invoices can be live for the same cent amount at once: the sub-cent tail runs 0.0001 to
# 0.0049. It stops below half a cent on purpose — a tail of 0.0056 rounds the total up to $5.21, and
# a buyer glancing at the price would see it move, which is the exact thing the tail replaced.
TAIL_SLOTS = 49


async def _unique_total(session: AsyncSession, amount_usd: float, now: datetime) -> float:
    """The exact USDT total this invoice must receive, distinct from every other recent invoice.

    The chain carries no order id, so a transfer is matched to an invoice by amount. Two buyers
    owing $5.20 at the same moment would hand the checker a payment it cannot attribute, and it
    refuses to guess — it logs "ambiguous" and credits neither.

    The distinguishing mark lives *below* the cent: $5.2043, not $5.20. USDT carries 18 decimals, so
    a four-decimal total is an ordinary amount to every wallet, and to the buyer the price is still
    $5.20 with a $0.20 fee.

    A tail stays taken for as long as the checker still matches transfers against its invoice —
    closed ones included (`invoices.LATE_CREDIT_MINUTES`). A buyer who cancels and pays anyway must
    still be recognised and credited; if their amount had already been handed to somebody else's new
    invoice, that late transfer would be ambiguous at best and credited to the wrong person at worst.
    """
    cutoff = now - timedelta(minutes=invoices.LATE_CREDIT_MINUTES)
    result = await session.execute(
        select(CryptoPayment.expected_amount).where(CryptoPayment.created_at >= cutoff)
    )
    taken = {round(float(a), 4) for a in result.scalars()}

    base = round(amount_usd + SERVICE_FEE, 2)
    free = [
        candidate
        for n in range(1, TAIL_SLOTS + 1)
        if (candidate := round(base + n / 10_000, 4)) not in taken
    ]
    if not free:
        # Every tail on this cent is spoken for — dozens of invoices for the identical amount inside
        # a quarter of an hour. Handing back the bare total is the honest fallback: it may end up
        # ambiguous and wait for an admin, which beats refusing to sell.
        return base
    # Picked at random rather than in order so a buyer cannot read the store's live order count off
    # their own invoice, and so two invoices opened in the same second rarely land adjacent.
    return random.choice(free)


async def render_payment_details(
    session: AsyncSession,
    user_id: int,
    amount_usd: float,
    locale: str,
    *,
    purchase_product_id: int | None = None,
    qty: int = 1,
) -> tuple[str, InlineKeyboardMarkup]:
    """Open an invoice and show the screen that pays it.

    `purchase_product_id` marks an invoice opened from a product's checkout; `qty` is how many units
    it buys. Both are recorded on the invoice, because when the transfer arrives it is the checker —
    not a button — that places this order.
    """
    monitor = BlockchainMonitor()
    now = datetime.now(UTC)

    total_amount = await _unique_total(session, amount_usd, now)
    # The fee shown is the fee charged — the flat one. What the tail adds is a fraction of a cent,
    # and calling that "service fee" would make the same product look differently priced to two
    # people standing next to each other.
    fee = SERVICE_FEE

    buying = purchase_product_id is not None
    payment = CryptoPayment(
        user_id=user_id,
        product_amount_minor=int(round(amount_usd * 100)),
        expected_amount=str(total_amount),  # Store total (what they actually send)
        currency="USDT",
        status="PENDING",
        description=(
            invoices.buy_description(purchase_product_id, amount_usd, qty)
            if buying
            else f"topup:{amount_usd}"
        ),
        created_at=now,
    )
    session.add(payment)
    await session.flush()

    minutes = PAYMENT_TIMEOUT_MINUTES
    heading = "💎 <b>USDT Payment · Order</b>" if buying else "💎 <b>USDT Payment · Wallet</b>"
    amount_label = "Order amount" if buying else "Top-up amount"
    text = (
        f"{heading}\n\n"
        f"<blockquote>🌐 Network: <b>BNB Smart Chain (BSC)</b>\n"
        f"🪙 Token: <b>USDT (BEP-20)</b>\n"
        f"📬 Address: <code>{monitor.wallet_address}</code></blockquote>\n\n"
        f"<blockquote>🧾 {amount_label}: ${amount_usd:.2f}\n"
        f"➕ Service fee: ${fee:.2f}\n"
        f"💰 Send exactly: <code>{total_amount:.4f}</code> <b>USDT</b></blockquote>\n\n"
        f"📋 <b>Tap Copy amount below</b> and paste it into your wallet's amount field. Don't type it "
        f"by hand and don't round it — the last few decimals are how we know this payment is "
        f"{'this order' if buying else 'yours'} and not somebody else's.\n\n"
        f"⏱️ This invoice expires in <b>{minutes} minutes</b>"
        + (" — your item is reserved for you until then.\n" if buying else ".\n")
        + (
            "✅ When the transfer arrives your order completes by itself and the item is sent to "
            "you right here.\n"
            if buying
            else "✅ It confirms automatically once the transfer arrives.\n"
        )
        + f"🎯 If your wallet or exchange takes its own fee, anything within ±${MATCH_TOLERANCE:.2f} "
        "still confirms — no need to send the difference."
    )
    if buying:
        text += (
            "\n\n⚠️ <b>Stay on this screen until it confirms.</b> ✖️ Cancel, 🏠 Menu, or any other "
            "button or command cancels this payment and puts the item back on sale. Copy amount, "
            "Copy address and 🔄 Check Payment keep it open. Money sent after the invoice closes "
            "isn't lost — it's added to your wallet balance."
        )

    from app.bot.keyboards.styles import NEUTRAL, SUCCESS, btn, copy_btn

    # Both halves of a payment are strings that must arrive intact, and both are ways payments get
    # lost: a mistyped address sends the money to nobody, and a mistyped amount arrives here as a
    # transfer we cannot attribute to a buyer. The buttons hand over the whole value, so neither
    # depends on the buyer selecting text accurately on a phone.
    rows = [
        [copy_btn(f"📋 Copy amount ({total_amount:.4f})", f"{total_amount:.4f}", NEUTRAL)],
        [copy_btn("📋 Copy address", monitor.wallet_address, NEUTRAL)],
        [btn("🔄 Check Payment", f"check_topup_crypto:{payment.id}", SUCCESS)],
        _exit_row(locale, payment.id),
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _exit_row(locale: str, payment_id: int) -> list[InlineKeyboardButton]:
    """Exit row for a live payment. Both buttons end the invoice: Cancel explicitly (and goes back to
    the product), Menu — like every other button or command — through the pending-payment rule in
    app/bot/middlewares/pending_payment.py. Either way the held item goes straight back on sale."""
    from app.bot.keyboards.styles import DANGER, PRIMARY, btn

    return [
        btn(t("menu.cancel", locale), f"cancel_topup_crypto:{payment_id}", DANGER),
        btn(t("menu.home", locale), NavCB(target="home").pack(), PRIMARY),
    ]


@router.callback_query(F.data.startswith("check_topup_crypto:"))
async def on_check_topup_payment(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Check payment status. One of the three buttons that keep the invoice open."""
    if not query.message:
        return

    payment_id = int(query.data.split(":")[-1])
    payment = await session.get(CryptoPayment, payment_id)

    if payment is None or payment.user_id != user.id:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return

    if payment.status == "PENDING":
        remaining = invoices.seconds_left(payment)
        if remaining > 0:
            # Nothing has changed yet, so the screen shouldn't change either — replacing it would
            # take away the wallet address and amount the user is still in the middle of paying.
            # A popup reports "not in yet" and leaves the invoice on screen to keep copying from.
            await query.answer(
                t("topup.not_received", user.locale, minutes=remaining // 60, seconds=remaining % 60),
                show_alert=True,
            )
            return

        payment.status = "EXPIRED"
        product_id = invoice_product_id(payment)
        if product_id is not None:
            await stock_hold_service.release(session, product_id, user.id)
        await session.flush()
        text = (
            "⌛ <b>Invoice expired</b>\n\n"
            f"The {PAYMENT_TIMEOUT_MINUTES}-minute payment window has closed and the item is back on "
            "sale. To try again, open the product and tap Buy Now.\n\n"
            "Already sent it? It isn't lost — once it arrives it's added to your wallet balance."
        )
    elif payment.status == "CONFIRMED":
        topup_amount = payment.product_amount_minor / 100
        order = await session.get(Order, payment.order_id) if payment.order_id else None
        if order is not None:
            text = (
                f"✅ <b>Payment received — order complete</b>\n\n"
                f"<blockquote>🛒 Order: <code>{order.order_number}</code>\n"
                f"🔗 Transaction: <code>{payment.tx_hash}</code></blockquote>\n\n"
                "Your item was sent to you in this chat. Thanks for choosing PowerX Digital!"
            )
        else:
            # `product_amount_minor` is the authoritative record of what the wallet was credited
            # with; the fee is flat, so it is stated rather than derived from the invoice total.
            text = (
                f"✅ <b>Payment received</b>\n\n"
                f"<blockquote>👛 Credited: ${topup_amount:.2f} USDT\n"
                f"➕ Service fee: ${SERVICE_FEE:.2f}\n"
                f"🔗 Transaction: <code>{payment.tx_hash}</code></blockquote>\n\n"
                "It's in your wallet balance. Thanks for choosing PowerX Digital!"
            )
    elif payment.status == "MISMATCH":
        text = (
            f"⚠️ <b>Amount doesn't match</b>\n\n"
            f"<blockquote>Expected: ${payment.expected_amount} USDT\n"
            f"Received: ${payment.actual_amount} USDT</blockquote>\n\n"
            "Please open a ticket in 🎧 Support and we'll sort it out."
        )
    elif payment.status in ("CANCELLED", "EXPIRED"):
        text = (
            "✖️ <b>This payment was closed</b>\n\n"
            "The item is back on sale. If you still sent the money, it's added to your wallet balance "
            "once it arrives."
        )
    else:
        text = f"ℹ️ <b>Payment status: {payment.status}</b>"

    from app.bot.keyboards.styles import DANGER, btn

    # Only settled payments reach here — a still-live one returns above with a popup. So there is
    # never anything left to cancel, and plain Back is the honest button: to the product it was
    # opened for, else to the wallet the money landed in.
    product_id = invoice_product_id(payment)
    back = (
        ProductCB(action="view", id=str(product_id)).pack()
        if product_id is not None
        else NavCB(target="profile").pack()
    )
    rows = [[btn(t("menu.back", user.locale), back, DANGER)]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()


@router.callback_query(F.data.startswith("cancel_topup_crypto:"))
async def on_cancel_topup_payment(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    """Close a pending invoice, put its item back on sale, and step back to the product."""
    if not query.message:
        return

    payment_id = int(query.data.split(":")[-1])
    payment = await session.get(CryptoPayment, payment_id)

    # Ownership is re-derived from the DB, never trusted from the callback payload — the id in it
    # is just a routing hint anyone could replay.
    if payment is None or payment.user_id != user.id:
        await query.answer(t("common.unknown_action", user.locale), show_alert=True)
        return

    # Only a live invoice changes state (and releases its held item). A payment that already
    # confirmed while the user was looking at the screen must not be cancelled out from under them.
    await invoices.cancel_invoice(session, payment)

    # Cancel means "one step back", and where back *is* depends on where the invoice came from. An
    # invoice opened from a product's checkout returns to that product.
    product_id = invoice_product_id(payment)
    if product_id is not None:
        from app.bot.handlers.products.browse import render_product_detail

        rendered = await render_product_detail(session, product_id, user.locale, user_id=user.id)
        if rendered is not None:
            text, markup = rendered
            await query.message.edit_text(text, reply_markup=markup)
            await query.answer(t("topup.cancelled", user.locale))
            return

    # A plain top-up invoice (only older ones exist — they can no longer be opened) or a product that
    # has since gone: the wallet is the nearest honest place to land.
    from app.bot.handlers.user.profile import render_wallet_screen

    text, markup = await render_wallet_screen(session, user)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer(t("topup.cancelled", user.locale))
