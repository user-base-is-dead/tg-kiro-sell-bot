"""Bring the database to head, whether it is brand new or an existing install.

One entrypoint for both cases, because getting it wrong in either direction is silently
destructive and the caller (docker-compose, a deploy script) has no way to tell them apart.

  Fresh database  -> build every table from the SQLAlchemy models, then `stamp` head.
                     `alembic upgrade head` CANNOT do this: it dies at revision 0007 with
                     `duplicate column name: description`, because the 0001 baseline already
                     creates a column that 0007 goes on to add. The chain is only replayable from
                     a database that predates the revision being applied.

  Existing database -> plain `alembic upgrade head`, which is exactly what it is for.
                     Creating tables from the models here would add missing TABLES but never
                     missing COLUMNS, and stamping head afterwards would then swear the schema is
                     current when it is not. That is the `no such column: warranties.claim_deadline_at`
                     failure: code expecting revision 0016 against a database still shaped like 0015.

"Existing" means the `alembic_version` table is present. A database with tables but no
`alembic_version` is neither case and is left alone rather than guessed at.

    python -m scripts.bootstrap_db
"""

from __future__ import annotations

import asyncio
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

import app.database.models  # noqa: F401  - importing registers every model on Base.metadata
from app.core.config import get_settings
from app.database.base import Base

ALEMBIC_INI = "alembic.ini"


async def _inspect_tables(database_url: str) -> list[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(lambda c: inspect(c).get_table_names())
    finally:
        await engine.dispose()


async def _create_all(database_url: str) -> int:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            return len(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()


def main() -> int:
    url = get_settings().database_url
    # Never print the URL itself: it carries the database password.
    print(f"Database bootstrap (dialect: {url.split(':', 1)[0]})")

    tables = asyncio.run(_inspect_tables(url))
    cfg = Config(ALEMBIC_INI)

    if "alembic_version" in tables:
        print(f"Existing install detected ({len(tables)} tables) - running migrations")
        command.upgrade(cfg, "head")
        print("OK: Migrations applied, database is at head")
        return 0

    if tables:
        print(f"ERROR: Found {len(tables)} tables but no alembic_version table.")
        print("ERROR: Refusing to guess. Either point DATABASE_URL at an empty database,")
        print("ERROR: or stamp the revision this schema actually matches and re-run.")
        return 1

    print("Fresh database - creating schema from models")
    table_count = asyncio.run(_create_all(url))
    print(f"OK: Schema created: {table_count} tables")

    command.stamp(cfg, "head")
    print("OK: Stamped at head - future migrations will apply on top")
    return 0


if __name__ == "__main__":
    sys.exit(main())
