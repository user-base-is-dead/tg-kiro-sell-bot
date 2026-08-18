from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models.crypto import CryptoPayment
from app.database.models.wallet import TxnType
from app.services.payments.blockchain_monitor import BlockchainMonitor
from app.services import wallet_service
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


async def check_crypto_payments(sessionmaker: async_sessionmaker) -> None:
    """Background job to check blockchain for pending payments."""
    monitor = BlockchainMonitor()

    try:
        transfers = await monitor.fetch_recent_transfers()
    except Exception as exc:
        # Errors here mean no payment can be detected at all, so this is never a warning: a
        # misconfigured or throttled RPC endpoint went unnoticed for exactly this reason once.
        logger.error("Crypto payment check failed — no payments can confirm: %s", exc)
        return

    async with sessionmaker() as session:
        # Get all pending crypto payments
        result = await session.execute(
            select(CryptoPayment).where(CryptoPayment.status == "PENDING")
        )
        pending = {str(p.id): p for p in result.scalars().all()}

        if not pending:
            return

        processed_txs = set()

        for tx in transfers:
            if tx["hash"] in processed_txs:
                continue

            # Two tiers, kept apart. `exact` is the buyer who sent the sub-cent tail we gave them —
            # that identifies one invoice and no other. `near` is the buyer whose wallet rounded the
            # amount, which is a real payment but has lost the tail that told the invoices apart.
            exact: list[tuple[str, CryptoPayment]] = []
            near: list[tuple[str, CryptoPayment]] = []
            for payment_id, payment in pending.items():
                # Skip if payment has expired
                if payment.created_at and datetime.now(UTC) > as_utc(payment.created_at) + timedelta(minutes=15):
                    payment.status = "EXPIRED"
                    await session.flush()
                    continue

                expected = float(payment.expected_amount)
                if monitor.matches_exactly(tx["value"], expected):
                    tier = exact
                elif monitor.matches_amount(tx["value"], expected):
                    tier = near
                else:
                    continue

                # Verify transfer happened after payment was created (allow 60s buffer)
                # `.timestamp()` on a naive datetime would read it as local time, shifting the
                # window by the host's UTC offset — hence as_utc first.
                if payment.created_at and tx["timestamp"] and tx["timestamp"] < as_utc(payment.created_at).timestamp() - 60:
                    continue

                tier.append((payment_id, payment))

            # An exact hit wins outright and is never weighed against rounded ones: the tail is
            # unique per invoice, so a buyer who paid what we asked is served immediately even while
            # somebody else's rounded transfer for the same cents is sitting in the same batch.
            matches = exact or near

            if not matches:
                continue

            if len(matches) > 1:
                # Two live invoices fit this transfer. Before giving up, ask who sent it: a buyer
                # who has paid from this wallet before, and whose wallet has never paid for anyone
                # else, is identified as surely as the amount would have identified them.
                sender_id = await _identify_sender(session, tx.get("from"))
                owned = [m for m in matches if m[1].user_id == sender_id] if sender_id else []
                if len(owned) != 1:
                    # Still ambiguous. Crediting the wrong buyer is worse than crediting nobody —
                    # this way both invoices stay open and an admin can settle it by hand, rather
                    # than one stranger getting the other's money and the loser having no recourse.
                    logger.error(
                        "Ambiguous payment tx %s (%.4f USDT) matched %d payments — skipping auto-credit.",
                        tx["hash"],
                        tx["value"],
                        len(matches),
                    )
                    continue
                matches = owned
                logger.info(
                    "Ambiguous tx %s resolved to user %d by sender address %s.",
                    tx["hash"],
                    sender_id,
                    tx.get("from"),
                )

            payment_id, payment = matches[0]

            # Credit wallet
            try:
                await wallet_service.credit(
                    session,
                    user_id=payment.user_id,
                    amount_minor=payment.product_amount_minor,
                    currency=payment.currency,
                    type_=TxnType.TOPUP,
                    idempotency_key=f"crypto:{payment.id}:{tx['hash']}",
                    ref_type="crypto_payment",
                    ref_id=str(payment.id),
                )

                # Mark payment as confirmed
                payment.status = "CONFIRMED"
                payment.actual_amount = str(tx["value"])
                payment.tx_hash = tx["hash"]
                # Lower-cased on the way in so the tiebreaker can compare addresses directly —
                # nodes are inconsistent about checksum casing and 0xAB… must not read as a
                # different wallet from 0xab….
                if tx.get("from"):
                    payment.from_address = tx["from"].lower()
                await session.flush()

                logger.info(
                    "Crypto payment confirmed for user %d: %.4f USDT (tx: %s)",
                    payment.user_id,
                    tx["value"],
                    tx["hash"],
                )
            except Exception as e:
                logger.error(f"Failed to credit wallet for payment {payment.id}: {e}")
                continue

            processed_txs.add(tx["hash"])
            pending.pop(payment_id, None)

        await session.commit()
