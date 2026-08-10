from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import stock_hold_service

logger = logging.getLogger(__name__)


async def expire_stock_holds(sessionmaker: async_sessionmaker) -> int:
    """Return every lapsed credential to the shelf. Returns how many were freed.

    The backend owns expiry, not the countdown in the buyer's chat: they can close Telegram, lose
    signal or simply never come back, and the credential still has to become buyable again. The read
    paths treat a lapsed hold as free too, so this job is what keeps the stored state tidy rather
    than what makes expiry correct — availability does not depend on how promptly it runs.
    """
    async with sessionmaker() as session:
        freed = await stock_hold_service.expire_due(session)
        await session.commit()
    if freed:
        logger.info("Released %s expired stock hold(s)", freed)
    return freed
