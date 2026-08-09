from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models.order import Warranty, WarrantyStatus
from app.database.session import session_scope

logger = logging.getLogger(__name__)


async def expire_warranties(sessionmaker: async_sessionmaker) -> int:
    """Flips ACTIVE warranties past their expires_at to EXPIRED. Idempotent — safe to run on
    any schedule, any number of times."""
    async with session_scope(sessionmaker) as session:
        now = datetime.now(UTC)
        result = await session.execute(
            select(Warranty.id).where(Warranty.status == WarrantyStatus.ACTIVE, Warranty.expires_at < now)
        )
        ids = [row[0] for row in result.all()]
        if not ids:
            return 0

        await session.execute(update(Warranty).where(Warranty.id.in_(ids)).values(status=WarrantyStatus.EXPIRED))
        logger.info("Expired %d warranties", len(ids))
        return len(ids)
