from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, BigIntPKMixin


class BotSetting(BigIntPKMixin, Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    value_json: Mapped[str] = mapped_column(Text)
    updated_by_admin_id: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
