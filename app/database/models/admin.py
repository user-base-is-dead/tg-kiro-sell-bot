from __future__ import annotations

import enum

from sqlalchemy import BigInteger, Enum
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, BigIntPKMixin, TimestampMixin


class AdminRole(str, enum.Enum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    SUPPORT = "SUPPORT"


class Admin(BigIntPKMixin, TimestampMixin, Base):
    __tablename__ = "admins"

    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    role: Mapped[AdminRole] = mapped_column(Enum(AdminRole, name="admin_role"), default=AdminRole.ADMIN)
    granted_by_id: Mapped[int | None] = mapped_column(BigInteger)
