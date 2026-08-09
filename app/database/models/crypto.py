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
    wallet_transaction_id: Mapped[int | None] = mapped_column(
        BigInteger
    )  # Reference to wallet transaction
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
