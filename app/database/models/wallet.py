from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, BigIntPKMixin, TimestampMixin


class TxnType(str, enum.Enum):
    TOPUP = "TOPUP"
    PURCHASE = "PURCHASE"
    REFUND = "REFUND"
    GIFT = "GIFT"
    REFERRAL = "REFERRAL"
    ADMIN_ADJUST = "ADMIN_ADJUST"


class TxnStatus(str, enum.Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Wallet(BigIntPKMixin, TimestampMixin, Base):
    __tablename__ = "wallets"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    balance_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    version: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped["User"] = relationship(back_populates="wallet")  # noqa: F821


class WalletTransaction(BigIntPKMixin, Base):
    __tablename__ = "wallet_transactions"

    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    type: Mapped[TxnType] = mapped_column(Enum(TxnType, name="txn_type"))
    amount_minor: Mapped[int] = mapped_column(Integer)
    balance_after_minor: Mapped[int] = mapped_column(Integer)
    status: Mapped[TxnStatus] = mapped_column(Enum(TxnStatus, name="txn_status"), default=TxnStatus.COMPLETED)
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    # PENDING top-ups only: proof the user submitted, and the admin's approve/reject note.
    proof: Mapped[str | None] = mapped_column(String(512))
    admin_note: Mapped[str | None] = mapped_column(String(512))
    reviewed_by_admin_id: Mapped[int | None] = mapped_column(BigInteger)
