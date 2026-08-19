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
    # The four Refund Wallet movements. Kept apart from REFUND above, which was the old
    # straight-back-to-spendable credit and stays valid for reading history.
    REFUND_PARK = "REFUND_PARK"  # a declined order's money arriving in the refund balance
    REFUND_PAYOUT = "REFUND_PAYOUT"  # an admin recording what they sent on chain
    REFUND_MOVE = "REFUND_MOVE"  # refund balance -> spendable balance (writes one row per side)
    REFUND_ADJUST = "REFUND_ADJUST"  # a hand correction to the refund balance


class TxnAccount(str, enum.Enum):
    """Which of a wallet's two balances a ledger row moved.

    MAIN is the spendable balance: what `debit()` charges and what a buyer can spend. REFUND is money
    owed back on a declined order. Splitting them on the ledger rather than in a second table means
    one query still reads a user's whole money history in order, and it makes the invariant checkable
    — sum the MAIN rows and you must get `balance_minor`, sum the REFUND rows and you must get
    `refund_balance_minor`.
    """

    MAIN = "MAIN"
    REFUND = "REFUND"


class TxnStatus(str, enum.Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Wallet(BigIntPKMixin, TimestampMixin, Base):
    __tablename__ = "wallets"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    balance_minor: Mapped[int] = mapped_column(Integer, default=0)
    # The Refund Wallet. A second balance on the same row, deliberately not reachable from
    # `wallet_service.debit` — which is the whole point: money owed back on a cancelled order must
    # not quietly become credit for another purchase. It leaves only by an admin recording a payout
    # or moving it across to `balance_minor`.
    refund_balance_minor: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    version: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped["User"] = relationship(back_populates="wallet")  # noqa: F821


class WalletTransaction(BigIntPKMixin, Base):
    __tablename__ = "wallet_transactions"

    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    type: Mapped[TxnType] = mapped_column(Enum(TxnType, name="txn_type"))
    # Which balance this row moved. Defaulted to MAIN so every existing row — and every existing
    # caller — keeps meaning exactly what it meant before the refund balance existed.
    account: Mapped[TxnAccount] = mapped_column(
        Enum(TxnAccount, name="txn_account"), default=TxnAccount.MAIN, server_default="MAIN"
    )
    amount_minor: Mapped[int] = mapped_column(Integer)
    # The balance of `account` after this row. Not the sum of both balances — reading a REFUND row's
    # running total against the spendable balance is how a ledger stops reconciling.
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
