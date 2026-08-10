"""Create the schema on a brand-new database, then mark the migration chain as applied.

`alembic upgrade head` does NOT work on an empty database: it dies at revision 0007 with
`duplicate column name: description`, because the 0001 baseline already creates a column that 0007
goes on to add. The chain is only replayable from a database that predates the revision you are
applying, so it is fine for upgrading an existing install and useless for creating a fresh one.

This does what a fresh install actually needs: build every table from the SQLAlchemy models (which
are the real source of truth for the current schema), then `stamp` the alembic version table at head
so future migrations apply cleanly on top.

Safe to re-run: `create_all` skips tables that already exist, and stamping is idempotent. It will
not upgrade an existing older database — use `alembic upgrade head` for that.

    python -m scripts.bootstrap_db
"""

from __future__ import annotations

import asyncio
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

import app.database.models  # noqa: F401  — importing registers every model on Base.metadata
from app.core.config import get_settings
from app.database.base import Base


async def _create_schema(database_url: str) -> tuple[int, bool]:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            existing = await conn.run_sync(lambda c: inspect(c).get_table_names())
            await conn.run_sync(Base.metadata.create_all)
            after = await conn.run_sync(lambda c: inspect(c).get_table_names())
    finally:
        await engine.dispose()
    return len(after), bool(existing)


def main() -> int:
    settings = get_settings()
    url = settings.database_url

    # Never print the URL: it carries the database password.
    print(f"Bootstrapping database (dialect: {url.split(':', 1)[0]})")

    table_count, had_tables = asyncio.run(_create_schema(url))
    if had_tables:
        print("WARN: Database already had tables - created any that were missing and left the rest alone.")
        print("WARN: If this is an existing install you want to UPGRADE, stop and run `alembic upgrade head`.")
    print(f"OK: Schema ready: {table_count} tables")

    command.stamp(Config("alembic.ini"), "head")
    print("OK: Alembic stamped at head - future migrations will apply on top")
    return 0


if __name__ == "__main__":
    sys.exit(main())
