from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.base import Base


@pytest_asyncio.fixture
async def sqlite_sessionmaker():
    """A fresh in-memory SQLite DB per test — fast, no external services required. Good
    enough for exercising service-layer logic (queries, transactions, constraints); the
    Postgres-specific FOR UPDATE SKIP LOCKED claim path still needs a real Postgres to verify
    concurrency (see test_stock_claim.py's note)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, expire_on_commit=False)

    await engine.dispose()
