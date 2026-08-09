from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, BigIntPKMixin, TimestampMixin


class Referral(BigIntPKMixin, TimestampMixin, Base):
    __tablename__ = "referrals"

    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    referee_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reward_minor: Mapped[int] = mapped_column(Integer, default=0)
    reward_txn_id: Mapped[int | None] = mapped_column(BigInteger)
