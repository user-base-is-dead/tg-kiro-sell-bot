"""The USDT checkout invoice: how long it lives, what it is for, and how it ends.

An invoice is opened when a buyer picks 💎 Pay with USDT. From that moment the credentials they are
buying are HELD — in checkout, off the shelf, not sold — for exactly as long as the invoice is open:
`PAYMENT_WINDOW_MINUTES`. Three endings, and only one of them sells anything:

  * paid inside the window  → the checker job places the order itself and the buyer gets the goods
                              in chat (no "tap Buy Now again"): HELD → DELIVERED, sold.
  * the window closes       → EXPIRED; the hold lapses and the credentials are back in stock.
  * the buyer leaves        → CANCELLED, by ✖️ Cancel or by doing anything else in the bot
                              (see app/bot/middlewares/pending_payment.py); the hold is released now.

Money that arrives for a closed invoice is never lost: the checker still matches transfers against
invoices closed within `LATE_CREDIT_MINUTES`, credits the buyer's wallet, and tells them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.crypto import CryptoPayment
from app.services import stock_hold_service
from app.utils.time import as_utc

# How long a buyer has to send the transfer. The credentials are held for the same five minutes
# (stock_hold_service.HOLD_MINUTES), so an invoice never outlives the stock it is selling.
PAYMENT_WINDOW_MINUTES = 5
# How far back the checker keeps matching transfers, closed invoices included. The chain itself is
# re-read on every run (blockchain_monitor.LOOKBACK_BLOCKS), so a transfer is normally seen within one
# 30-second run of being mined; this bounds how late a transfer can be and still be recognised.
LATE_CREDIT_MINUTES = 15
# A transfer stamped this much after the window closed still counts as sent in time: block times and
# our clock are not the same clock.
_CLOCK_SLACK = timedelta(seconds=60)


def window_end(payment: CryptoPayment) -> datetime:
    return as_utc(payment.created_at) + timedelta(minutes=PAYMENT_WINDOW_MINUTES)


def seconds_left(payment: CryptoPayment, now: datetime | None = None) -> int:
    return max(0, int((window_end(payment) - (now or datetime.now(UTC))).total_seconds()))


def sent_in_time(payment: CryptoPayment, tx_timestamp: float | None) -> bool:
    """Whether a transfer was made while the invoice was still open."""
    if tx_timestamp is None:
        return False
    return datetime.fromtimestamp(tx_timestamp, UTC) <= window_end(payment) + _CLOCK_SLACK


def _parts(payment: CryptoPayment) -> list[str]:
    return (payment.description or "").split(":")


def invoice_product_id(payment: CryptoPayment) -> int | None:
    """The product this invoice was opened to buy, or None for a plain wallet top-up.

    Stored in `description` as `buy:<product_id>:<amount>[:<qty>]`; a bare top-up keeps the old
    `topup:<amount>` form. An unparseable value degrades to "this was a top-up", the safe answer.
    """
    parts = _parts(payment)
    if len(parts) >= 2 and parts[0] == "buy":
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


def invoice_quantity(payment: CryptoPayment) -> int:
    """How many units the invoice is for. Invoices from before the quantity was recorded are 1."""
    parts = _parts(payment)
    if len(parts) >= 4 and parts[0] == "buy":
        try:
            return max(1, int(parts[3]))
        except ValueError:
            return 1
    return 1


def buy_description(product_id: int, amount_usd: float, qty: int) -> str:
    return f"buy:{product_id}:{amount_usd}:{qty}"


async def live_buy_invoice(session: AsyncSession, user_id: int) -> CryptoPayment | None:
    """This buyer's open checkout invoice, if the window has not closed yet."""
    cutoff = datetime.now(UTC) - timedelta(minutes=PAYMENT_WINDOW_MINUTES)
    result = await session.execute(
        select(CryptoPayment)
        .where(
            CryptoPayment.user_id == user_id,
            CryptoPayment.status == "PENDING",
            CryptoPayment.description.like("buy:%"),
            CryptoPayment.created_at >= cutoff,
        )
        .order_by(CryptoPayment.id.desc())
    )
    return result.scalars().first()


async def cancel_invoice(session: AsyncSession, payment: CryptoPayment) -> bool:
    """Close a still-open invoice and put its held credentials straight back in stock.

    Returns False — and changes nothing — when the invoice is no longer PENDING: one that confirmed a
    moment ago must never be cancelled out from under the buyer who just paid it.
    """
    if payment.status != "PENDING":
        return False
    payment.status = "CANCELLED"
    product_id = invoice_product_id(payment)
    if product_id is not None:
        await stock_hold_service.release(session, product_id, payment.user_id)
    await session.flush()
    return True


async def cancel_live_invoice(session: AsyncSession, user_id: int) -> CryptoPayment | None:
    """Cancel this buyer's open checkout invoice, if they have one. Returns the one cancelled."""
    payment = await live_buy_invoice(session, user_id)
    if payment is not None and await cancel_invoice(session, payment):
        return payment
    return None
