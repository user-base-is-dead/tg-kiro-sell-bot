from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, BigIntPKMixin


class AuditLog(BigIntPKMixin, Base):
    __tablename__ = "audit_logs"

    actor_telegram_id: Mapped[int] = mapped_column(BigInteger)
    actor_role: Mapped[str | None] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[str | None] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_audit_actor_created", "actor_telegram_id", "created_at"),
        Index("ix_audit_action_created", "action", "created_at"),
    )
