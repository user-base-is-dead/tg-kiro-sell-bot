from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# BIGINT on Postgres (production); plain INTEGER on every other dialect. SQLite only treats
# an exact "INTEGER PRIMARY KEY" column as its autoincrementing rowid alias — a bare BIGINT
# there silently fails to autopopulate — so this variant is what makes the same models usable
# against a real Postgres deployment AND a throwaway SQLite DB in tests.
_BIGINT_PK = BigInteger().with_variant(Integer(), "sqlite")


class BigIntPKMixin:
    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)


def new_uuid() -> str:
    return str(uuid.uuid4())
