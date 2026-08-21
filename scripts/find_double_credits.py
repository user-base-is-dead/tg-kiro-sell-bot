"""Find on-chain transfers that were credited to the wallet more than once.

Run it on the machine the bot runs on, with the same `.env`:

    python -m scripts.find_double_credits

One USDT transfer can only ever pay for one invoice. Until the checker keyed its ledger entry on
the transaction hash, it keyed it on the invoice instead — so a buyer who had two invoices live at
once (pressed 💎 Pay with Crypto twice, or topped up and then bought) and paid once could be
credited once per invoice, because the chain re-offers the same transfer for ~15 minutes and the
±$0.03 `near` window is far wider than the sub-cent tail that tells two invoices apart.

This lists every transfer that landed on more than one invoice, and what each credit was worth, so
the balances can be put right by hand. Read-only — it never writes, so it is safe against
production.
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict

from sqlalchemy import select

from app.core.config import get_settings
from app.database.models.crypto import CryptoPayment
from app.database.models.order import Order
from app.database.models.user import User
from app.database.models.wallet import Wallet, WalletTransaction
from app.database.session import build_engine, build_sessionmaker


def _usd(minor: int) -> str:
    return f"${minor / 100:.2f}"


async def _run() -> int:
    settings = get_settings()
    url = settings.database_url
    # Never print the URL itself: it carries the database password.
    print(f"Double-credit scan (dialect: {url.split(':', 1)[0]})")
    print()

    engine = build_engine(url)
    sessionmaker = build_sessionmaker(engine)
    try:
        async with sessionmaker() as session:
            result = await session.execute(
                select(CryptoPayment).where(CryptoPayment.tx_hash.is_not(None))
            )
            by_hash: dict[str, list[CryptoPayment]] = defaultdict(list)
            for payment in result.scalars():
                by_hash[(payment.tx_hash or "").lower()].append(payment)

            duplicates = {h: ps for h, ps in by_hash.items() if len(ps) > 1}
            print(f"confirmed invoices with a transaction hash: {sum(len(p) for p in by_hash.values())}")
            print(f"distinct transfers: {len(by_hash)}")
            print(f"transfers used more than once: {len(duplicates)}")
            print()

            if not duplicates:
                print("RESULT: CLEAN - every transfer paid for exactly one invoice.")
                return 0

            overpaid_minor = 0
            for tx_hash, payments in sorted(duplicates.items()):
                payments.sort(key=lambda p: p.id)
                # Everything after the first credit is money the shop never received.
                extra = sum(p.product_amount_minor for p in payments[1:])
                overpaid_minor += extra

                print("=" * 78)
                print(f"tx {tx_hash}")
                print(f"  credited {len(payments)} times - {_usd(extra)} more than was ever received")
                for payment in payments:
                    user = await session.get(User, payment.user_id)
                    who = f"@{user.username}" if user and user.username else f"user id {payment.user_id}"
                    tg = f" (telegram {user.telegram_id})" if user else ""
                    order_number = ""
                    if payment.order_id:
                        order = await session.get(Order, payment.order_id)
                        order_number = f"  -> order {order.order_number}" if order else ""
                    print(
                        f"  invoice #{payment.id}  {who}{tg}  {_usd(payment.product_amount_minor)}"
                        f"  [{payment.status}]  {payment.description or ''}"
                        f"  expected {payment.expected_amount}"
                        f"  got {payment.actual_amount}{order_number}"
                    )

                # The ledger rows behind those credits, so the amount to claw back is not a guess.
                keys = [f"crypto:{p.id}:{tx_hash}" for p in payments]
                keys += [f"crypto:tx:{tx_hash}"]
                txns = await session.execute(
                    select(WalletTransaction, Wallet)
                    .join(Wallet, Wallet.id == WalletTransaction.wallet_id)
                    .where(WalletTransaction.idempotency_key.in_(keys))
                    .order_by(WalletTransaction.id)
                )
                for txn, wallet in txns.all():
                    print(
                        f"    ledger txn#{txn.id}  wallet {wallet.id} (user {wallet.user_id})"
                        f"  {_usd(txn.amount_minor)}  balance after {_usd(txn.balance_after_minor)}"
                    )
                print()

            print("=" * 78)
            print(f"RESULT: {len(duplicates)} transfer(s) credited more than once.")
            print(f"Total credited beyond what was received: {_usd(overpaid_minor)}")
            print()
            print(
                "Nothing has been changed. Settle each one by hand - /adjust_balance to claw back a "
                "balance that was never paid for, or decline the extra order if it is still "
                "unfulfilled."
            )
            return 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
