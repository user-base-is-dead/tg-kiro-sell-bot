"""Say why a screen answers "Something went wrong on our end", without reading the log.

Run it on the machine the bot runs on, with the same `.env`:

    python -m scripts.doctor

It reports what the live database is missing relative to this checkout's models, and where its
alembic revision sits. Read-only — it never issues DDL, so it is safe against production.
"""

from __future__ import annotations

import asyncio
import sys

from app.core.config import get_settings
from app.database.schema_guard import check_schema
from app.database.session import build_engine


async def _run() -> int:
    settings = get_settings()
    url = settings.database_url
    # Never print the URL itself: it carries the database password.
    print(f"Database doctor (dialect: {url.split(':', 1)[0]})")

    engine = build_engine(url)
    try:
        report = await check_schema(engine)
    finally:
        await engine.dispose()

    print(f"alembic revision in database: {report.db_revision or '(none)'}")
    print(f"alembic head in this checkout: {report.head_revision or '(unknown)'}")
    print()
    print(report.describe())
    print()

    if report.is_broken:
        print("RESULT: BROKEN - screens touching the tables above will fail at runtime.")
        print("Fix:  alembic upgrade head        (existing database)")
        print("      python -m scripts.bootstrap_db   (picks the right path for new vs existing)")
        return 1
    if report.revision_behind:
        print("RESULT: OK for now, but migrations are pending - run `alembic upgrade head`.")
        return 0
    print("RESULT: OK - the database matches the models.")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
