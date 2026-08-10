from __future__ import annotations

from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    """Normalise a datetime read back from the database to timezone-aware UTC.

    `DateTime(timezone=True)` is honoured by Postgres but not by SQLite, which has no native
    timezone storage and hands back naive datetimes. Since every timestamp is written as UTC, a
    naive value read back *is* UTC and only needs the tzinfo reattached. Anything that compares or
    subtracts a stored timestamp has to go through here — mixing the two raises TypeError, and
    calling `.timestamp()` on a naive value silently reinterprets it as local time instead.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
