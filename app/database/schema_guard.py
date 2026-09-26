"""Does the live database actually have the shape this code expects?

Every "⚠️ Something went wrong on our end" that appears on a screen which works fine in tests has
the same shape: the code was updated, the schema was not. `SELECT ... FROM products` names every
column the model declares, so one missing column takes out an entire screen — the store, the
orders list — with a `ProgrammingError`/`OperationalError` that only exists in the server's log.
The shopper gets the generic alert and nobody learns why.

This module answers the question at boot instead, by comparing the SQLAlchemy metadata with what
the database inspector reports:

  * a missing TABLE or COLUMN is definite breakage — some screen is guaranteed to raise;
  * `alembic_version` behind the migration head is the usual cause, and is reported alongside so
    the fix is obvious (`alembic upgrade head`).

Only absences are reported. Type/nullability differences are deliberately ignored: they rarely
break a query outright and comparing them across dialects produces false alarms, which would be
worse than no check at all in a guard that can stop the bot from starting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

import app.database.models  # noqa: F401 - importing registers every model on Base.metadata
from app.database.base import Base

logger = logging.getLogger(__name__)

ALEMBIC_INI = "alembic.ini"


@dataclass
class SchemaReport:
    """What the database is missing, relative to the models this process is running."""

    missing_tables: list[str] = field(default_factory=list)
    # table -> columns the model declares and the database does not have
    missing_columns: dict[str, list[str]] = field(default_factory=dict)
    db_revision: str | None = None
    head_revision: str | None = None

    @property
    def is_broken(self) -> bool:
        """True when some query is guaranteed to fail — not merely "migrations are pending"."""
        return bool(self.missing_tables or self.missing_columns)

    @property
    def revision_behind(self) -> bool:
        return (
            self.head_revision is not None
            and self.db_revision is not None
            and self.db_revision != self.head_revision
        )

    def describe(self) -> str:
        lines: list[str] = []
        if self.missing_tables:
            lines.append(f"missing tables: {', '.join(sorted(self.missing_tables))}")
        for table in sorted(self.missing_columns):
            lines.append(f"missing columns on {table}: {', '.join(self.missing_columns[table])}")
        if self.revision_behind:
            lines.append(
                f"alembic_version is at {self.db_revision}, code expects {self.head_revision}"
            )
        elif self.db_revision is None:
            lines.append("no alembic_version table - this database was never stamped")
        return "\n".join(lines) or "schema matches the models"


def _head_revision() -> str | None:
    """The revision this checkout's migrations end at, or None if alembic isn't configured here."""
    try:
        return ScriptDirectory.from_config(Config(ALEMBIC_INI)).get_current_head()
    except Exception as exc:  # noqa: BLE001 - a missing alembic.ini must not stop the bot
        logger.debug("Could not read alembic head: %s", exc)
        return None


def _inspect(conn) -> tuple[list[str], dict[str, list[str]], str | None]:
    inspector = inspect(conn)
    existing = set(inspector.get_table_names())

    missing_tables: list[str] = []
    missing_columns: dict[str, list[str]] = {}

    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            missing_tables.append(table.name)
            continue
        have = {col["name"] for col in inspector.get_columns(table.name)}
        absent = [c.name for c in table.columns if c.name not in have]
        if absent:
            missing_columns[table.name] = absent

    db_revision: str | None = None
    if "alembic_version" in existing:
        row = conn.exec_driver_sql("SELECT version_num FROM alembic_version").fetchone()
        db_revision = row[0] if row else None

    return missing_tables, missing_columns, db_revision


async def check_schema(engine: AsyncEngine) -> SchemaReport:
    async with engine.connect() as conn:
        missing_tables, missing_columns, db_revision = await conn.run_sync(_inspect)
    return SchemaReport(
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        db_revision=db_revision,
        head_revision=_head_revision(),
    )
