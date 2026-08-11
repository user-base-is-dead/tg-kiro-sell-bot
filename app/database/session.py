from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    # SQLite serializes every write behind a single file lock, so a connection pool buys nothing
    # there (and QueuePool sizing args are meaningless for it). Only real servers get a tuned pool.
    if database_url.startswith("sqlite"):
        return create_async_engine(database_url, echo=echo, pool_pre_ping=True)

    # Under heavy concurrent traffic each in-flight update holds one session (one connection) for
    # its whole lifetime. SQLAlchemy's defaults (pool_size=5, max_overflow=10 -> 15 total) exhaust
    # almost immediately and every extra update blocks up to pool_timeout, then errors — which the
    # user sees as Telegram's generic failure dialog. 20+40 gives one process up to 60 concurrent
    # connections (well under Postgres' default max_connections of 100).
    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=40,
        pool_timeout=30,
        pool_recycle=1800,  # recycle before Postgres/proxies drop idle connections
    )


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
