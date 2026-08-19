from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, BigIntPKMixin, TimestampMixin


class CryptoPayment(BigIntPKMixin, TimestampMixin, Base):
    """Tracks cryptocurrency payment attempts and confirmations with user isolation."""

    __tablename__ = "crypto_payments"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_amount_minor: Mapped[int] = mapped_column(
        BigInteger
    )  # Amount for product (cents/satoshis)
    expected_amount: Mapped[str] = mapped_column(String(64))  # Amount we expect to receive
    actual_amount: Mapped[str | None] = mapped_column(String(64))  # Amount actually received
    fee_amount: Mapped[str | None] = mapped_column(String(64))  # Fee deducted
    currency: Mapped[str] = mapped_column(String(16))  # BTC, ETH, USDC, etc
    status: Mapped[str] = mapped_column(
        String(32), default="PENDING"
    )  # PENDING, CONFIRMED, MISMATCH, COMPLETED
    description: Mapped[str | None] = mapped_column(String(512))
    tx_hash: Mapped[str | None] = mapped_column(String(256), index=True)  # Blockchain tx
    # The address the confirmed transfer came from. Recorded so a returning buyer's wallet can break
    # a tie when two invoices are for the same amount — see `crypto_payment_checker._identify_sender`
    # for why it is only ever a tiebreaker and never proof on its own.
    from_address: Mapped[str | None] = mapped_column(String(64), index=True)
    wallet_transaction_id: Mapped[int | None] = mapped_column(
        BigInteger
    )  # Reference to wallet transaction
    # The order this confirmed invoice ended up paying for, set once by `_claim_crypto_invoice`. It is
    # what lets a refund conversation quote the on-chain transaction the buyer is looking at, and the
    # NULL check on it is what stops one payment being counted as funding two separate orders.
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), index=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
