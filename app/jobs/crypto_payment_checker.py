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

            matches = []
            for payment_id, payment in pending.items():
                # Skip if payment has expired
                if payment.created_at and datetime.now(UTC) > as_utc(payment.created_at) + timedelta(minutes=15):
                    payment.status = "EXPIRED"
                    await session.flush()
                    continue

                expected = float(payment.expected_amount)
                if not monitor.matches_amount(tx["value"], expected):
                    continue

                # Verify transfer happened after payment was created (allow 60s buffer)
                # `.timestamp()` on a naive datetime would read it as local time, shifting the
                # window by the host's UTC offset — hence as_utc first.
                if payment.created_at and tx["timestamp"] and tx["timestamp"] < as_utc(payment.created_at).timestamp() - 60:
                    continue

                matches.append((payment_id, payment))

            if not matches:
                continue

            if len(matches) > 1:
                # Ambiguous - log and skip
                logger.error(
                    "Ambiguous payment tx %s (%.4f USDT) matched %d payments — skipping auto-credit.",
                    tx["hash"],
                    tx["value"],
                    len(matches),
                )
                continue

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
