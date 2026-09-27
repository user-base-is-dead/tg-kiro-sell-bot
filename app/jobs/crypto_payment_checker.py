from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models.crypto import CryptoPayment
from app.database.models.wallet import TxnType
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.user_repo import UserRepo
from app.locales.i18n import t
from app.services import order_service, purchase_service, stock_hold_service, wallet_service
from app.services.payments import invoices
from app.services.payments.blockchain_monitor import BlockchainMonitor
from app.utils.errors import UserError
from app.utils.text import escape_html
from app.utils.time import as_utc

logger = logging.getLogger(__name__)


async def _identify_sender(session, from_address: str | None) -> int | None:
    """The one user this address belongs to, or None if it does not identify anybody.

    A transfer carries two identifying things: how much, and who sent it. The amount is the primary
    match; this is the fallback for when the amount alone fits more than one live invoice.

    "Belongs to" has to be strict. Most buyers pay from a personal wallet, and that address is as
    good as a name. But a withdrawal straight from an exchange leaves from the exchange's shared hot
    wallet, and thousands of unrelated people share it — treating that as identity would hand one
    buyer's money to whoever paid from Binance last. So the address counts only while every payment
    ever confirmed from it belongs to the same account; the moment a second account uses it, it goes
    back to proving nothing, permanently and for everyone.
    """
    if not from_address:
        return None
    result = await session.execute(
        select(CryptoPayment.user_id)
        .where(
            CryptoPayment.from_address == from_address.lower(),
            CryptoPayment.status.in_(("CONFIRMED", "COMPLETED")),
        )
        .distinct()
    )
    owners = result.scalars().all()
    return owners[0] if len(owners) == 1 else None


async def _spent_tx_hashes(session, hashes: list[str]) -> set[str]:
    """Of these transfers, the ones that have already paid for an invoice.

    A transfer is spent exactly once. That has to be checked against the database and not just
    against what this run has already done, because `fetch_recent_transfers` deliberately re-reads a
    ~15 minute window of the chain every 30 seconds: the same transfer is offered to this job
    dozens of times, and only the first offer must be worth anything.
    """
    if not hashes:
        return set()
    wanted = {h.lower() for h in hashes}
    result = await session.execute(
        select(CryptoPayment.tx_hash).where(CryptoPayment.tx_hash.in_(wanted))
    )
    return {h.lower() for h in result.scalars() if h}


async def check_crypto_payments(sessionmaker: async_sessionmaker, bot: Bot | None = None) -> None:
    """Match incoming USDT transfers to invoices, and finish the purchases they pay for.

    Every run, in order:
      1. Invoices whose payment window has closed become EXPIRED and their held items go back on
         sale — this runs even when the chain can't be read.
      2. Transfers are matched against every invoice opened in the last `LATE_CREDIT_MINUTES`,
         closed ones included, so money that arrives late is still recognised. A match credits the
         buyer's wallet and marks the invoice CONFIRMED.
      3. After that is committed, each checkout paid in time is turned into the order itself — the
         buyer gets the goods in chat without pressing anything — and every other credit is
         explained to the buyer. Placing the order is its own transaction: if it fails (sold out,
         say), the money is already safely in their wallet.
    """
    now = datetime.now(UTC)
    closed: dict[int, tuple[int, int | None]] = {}

    async with sessionmaker() as session:
        result = await session.execute(select(CryptoPayment).where(CryptoPayment.status == "PENDING"))
        for payment in result.scalars().all():
            if payment.created_at and now > invoices.window_end(payment):
                payment.status = "EXPIRED"
                product_id = invoices.invoice_product_id(payment)
                if product_id is not None:
                    await stock_hold_service.release(session, product_id, payment.user_id)
                closed[payment.id] = (payment.user_id, product_id)
        await session.commit()

    monitor = BlockchainMonitor()
    try:
        transfers = await monitor.fetch_recent_transfers()
    except Exception as exc:
        # Errors here mean no payment can be detected at all, so this is never a warning: a
        # misconfigured or throttled RPC endpoint went unnoticed for exactly this reason once.
        logger.error("Crypto payment check failed — no payments can confirm: %s", exc)
        transfers = []

    paid: list[_Paid] = []
    if transfers:
        async with sessionmaker() as session:
            paid = await _match_transfers(session, monitor, transfers, now)
            await session.commit()

    fulfilled = {p.payment_id for p in paid}
    for item in paid:
        await _finish(sessionmaker, bot, item)
    for payment_id, (user_id, product_id) in closed.items():
        if payment_id not in fulfilled and product_id is not None:
            await _tell_window_closed(sessionmaker, bot, user_id, product_id)


@dataclass(frozen=True)
class _Paid:
    payment_id: int
    user_id: int
    amount_minor: int
    product_id: int | None
    qty: int
    # Paid for a checkout that was still open (or had only just closed by the clock) — as opposed to
    # one the buyer cancelled or a transfer sent after the window.
    on_time: bool


async def _match_transfers(session, monitor: BlockchainMonitor, transfers: list[dict], now: datetime) -> list[_Paid]:
    cutoff = now - timedelta(minutes=invoices.LATE_CREDIT_MINUTES)
    result = await session.execute(
        select(CryptoPayment).where(
            CryptoPayment.status.in_(("PENDING", "EXPIRED", "CANCELLED")),
            CryptoPayment.created_at >= cutoff,
        )
    )
    candidates = {str(p.id): p for p in result.scalars().all()}
    if not candidates:
        return []

    # Transfers that already paid for something, on an earlier run of this job. Without this, a
    # buyer who opened two invoices and paid once got credited twice: the same transfer is still on
    # chain 30 seconds later and the second invoice's tail is well inside the ±$0.03 `near` window.
    spent = await _spent_tx_hashes(session, [tx["hash"] for tx in transfers])

    paid: list[_Paid] = []
    processed_txs = set()
    for tx in transfers:
        if tx["hash"] in processed_txs or tx["hash"].lower() in spent:
            continue

        # Two tiers, kept apart. `exact` is the buyer who sent the sub-cent tail we gave them — that
        # identifies one invoice and no other. `near` is the buyer whose wallet rounded the amount,
        # which is a real payment but has lost the tail that told the invoices apart.
        exact: list[tuple[str, CryptoPayment]] = []
        near: list[tuple[str, CryptoPayment]] = []
        for payment_id, payment in candidates.items():
            expected = float(payment.expected_amount)
            if monitor.matches_exactly(tx["value"], expected):
                tier = exact
            elif monitor.matches_amount(tx["value"], expected):
                tier = near
            else:
                continue

            # Verify transfer happened after payment was created (allow 60s buffer). `.timestamp()`
            # on a naive datetime would read it as local time, hence as_utc first.
            if payment.created_at and tx["timestamp"] and tx["timestamp"] < as_utc(payment.created_at).timestamp() - 60:
                continue

            tier.append((payment_id, payment))

        # An exact hit wins outright and is never weighed against rounded ones: the tail is unique
        # per invoice, so a buyer who paid what we asked is served immediately.
        matches = exact or near
        if not matches:
            continue

        if len(matches) > 1:
            # Two invoices fit this transfer. Before giving up, ask who sent it: a buyer who has paid
            # from this wallet before, and whose wallet has never paid for anyone else, is identified
            # as surely as the amount would have identified them.
            sender_id = await _identify_sender(session, tx.get("from"))
            owned = [m for m in matches if m[1].user_id == sender_id] if sender_id else []
            if len(owned) != 1:
                # Still ambiguous. Crediting the wrong buyer is worse than crediting nobody — this
                # way the invoices stay as they are and an admin can settle it by hand.
                logger.error(
                    "Ambiguous payment tx %s (%.4f USDT) matched %d payments — skipping auto-credit.",
                    tx["hash"],
                    tx["value"],
                    len(matches),
                )
                continue
            matches = owned
            logger.info("Ambiguous tx %s resolved to user %d by sender address %s.", tx["hash"], sender_id, tx.get("from"))

        payment_id, payment = matches[0]
        on_time = payment.status in ("PENDING", "EXPIRED") and invoices.sent_in_time(payment, tx["timestamp"])

        try:
            await wallet_service.credit(
                session,
                user_id=payment.user_id,
                amount_minor=payment.product_amount_minor,
                currency=payment.currency,
                type_=TxnType.TOPUP,
                # Keyed on the transfer, NOT on the invoice it was matched to: a transaction hash is
                # unique on chain and the money behind it can only be spent once.
                idempotency_key=f"crypto:tx:{tx['hash'].lower()}",
                ref_type="crypto_payment",
                ref_id=str(payment.id),
            )
            payment.status = "CONFIRMED"
            payment.actual_amount = str(tx["value"])
            # Lower-cased: this column is what `_spent_tx_hashes` reads back to decide a transfer is
            # spent, and a hash that round-trips in a different case would read as unspent.
            payment.tx_hash = tx["hash"].lower()
            payment.confirmed_at = datetime.now(UTC)
            if tx.get("from"):
                payment.from_address = tx["from"].lower()
            await session.flush()
            logger.info(
                "Crypto payment confirmed for user %d: %.4f USDT (tx: %s, %s)",
                payment.user_id,
                tx["value"],
                tx["hash"],
                "in time" if on_time else "late — wallet credit only",
            )
        except Exception as e:
            logger.error(f"Failed to credit wallet for payment {payment.id}: {e}")
            continue

        paid.append(
            _Paid(
                payment_id=payment.id,
                user_id=payment.user_id,
                amount_minor=payment.product_amount_minor,
                product_id=invoices.invoice_product_id(payment),
                qty=invoices.invoice_quantity(payment),
                on_time=on_time,
            )
        )
        processed_txs.add(tx["hash"])
        spent.add(tx["hash"].lower())
        candidates.pop(payment_id, None)
    return paid


async def _finish(sessionmaker: async_sessionmaker, bot: Bot | None, item: _Paid) -> None:
    """Turn a checkout paid in time into its order, or tell the buyer where their money went."""
    credited = f"${item.amount_minor / 100:.2f}"
    async with sessionmaker() as session:
        user = await UserRepo(session).get_by_id(item.user_id)
        if user is None:
            return
        # Read now: a rollback below expires every loaded row.
        locale, chat_id = user.locale, user.chat_id or user.telegram_id

        if not (item.on_time and item.product_id is not None):
            await _dm(
                bot,
                chat_id,
                "💎 <b>Payment received</b>\n\n"
                f"<b>{credited}</b> arrived after this checkout was closed, so no order was placed — "
                "it's been added to your 👛 Wallet balance. Tap Buy Now on the product to use it, or "
                "ask 🎧 Support if anything looks wrong.",
            )
            return

        try:
            placed = await order_service.place_order(
                session,
                user_id=item.user_id,
                product_id=item.product_id,
                qty=item.qty,
                # One order per invoice. The default key is a 15-second double-tap guard, and two
                # invoices for the same product paid in one run would share it — the second payment
                # would be handed the first order back instead of buying its own.
                idempotency_key=f"order:crypto:{item.payment_id}",
                crypto_payment_id=item.payment_id,
            )
            await session.commit()
        except UserError as exc:
            await session.rollback()
            await _dm(
                bot,
                chat_id,
                "💎 <b>Payment received</b>\n\n"
                f"<b>{credited}</b> has been added to your 👛 Wallet balance, but the order couldn't be "
                f"completed automatically: {t(exc.i18n_key, locale)}\n\n"
                "Your balance is safe — tap Buy Now to try again, or ask 🎧 Support.",
            )
            return
        except Exception as exc:  # noqa: BLE001 - the money is credited; never lose the buyer's message
            await session.rollback()
            logger.error("Placing the paid order for payment %s failed: %s", item.payment_id, exc)
            await _dm(
                bot,
                chat_id,
                "💎 <b>Payment received</b>\n\n"
                f"<b>{credited}</b> has been added to your 👛 Wallet balance. The order didn't go "
                "through automatically — tap Buy Now to finish it, or ask 🎧 Support.",
            )
            return

        text = await purchase_service.buyer_message(session, placed, locale)
        await _dm(bot, chat_id, "💎 <b>Payment received — thank you!</b>\n\n" + text)
        if bot is not None:
            try:
                await purchase_service.after_purchase(bot, session, placed)
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - the sale is done; the group post is not
                await session.rollback()
                logger.error("After-purchase steps failed for %s: %s", placed.order.order_number, exc)


async def _tell_window_closed(
    sessionmaker: async_sessionmaker, bot: Bot | None, user_id: int, product_id: int
) -> None:
    async with sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        product = await ProductRepo(session).get_by_id(product_id)
        if user is None:
            return
        name = escape_html(product.name) if product is not None else "your item"
        await _dm(
            bot,
            user.chat_id or user.telegram_id,
            f"⌛ <b>Payment time is up</b>\n\n"
            f"The {invoices.PAYMENT_WINDOW_MINUTES}-minute window to pay for <b>{name}</b> has closed "
            "and it's back on sale. To buy it, open the product and tap Buy Now again.\n\n"
            "Already sent the money? It isn't lost — once it arrives it's added to your wallet "
            "balance.",
        )


async def _dm(bot: Bot | None, chat_id: int, text: str) -> None:
    if bot is None:
        return
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError as exc:
        logger.warning("Couldn't message user %s about their payment (%s)", chat_id, exc)
