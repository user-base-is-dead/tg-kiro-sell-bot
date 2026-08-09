from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, BigIntPKMixin, TimestampMixin


class GiftStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    EXPIRED = "EXPIRED"
    EXHAUSTED = "EXHAUSTED"


class GiftCode(BigIntPKMixin, TimestampMixin, Base):
    __tablename__ = "gift_codes"

    code_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    code_last4: Mapped[str] = mapped_column(String(4))
    value_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[GiftStatus] = mapped_column(Enum(GiftStatus, name="gift_status"), default=GiftStatus.ACTIVE)
    created_by_admin_id: Mapped[int] = mapped_column(BigInteger)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str | None] = mapped_column(String(512))


class GiftRedemption(BigIntPKMixin, Base):
    __tablename__ = "gift_redemptions"

    gift_code_id: Mapped[int] = mapped_column(ForeignKey("gift_codes.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    wallet_transaction_id: Mapped[int] = mapped_column(ForeignKey("wallet_transactions.id"))
    redeemed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ux_gift_redemptions_code_user", "gift_code_id", "user_id", unique=True),)
