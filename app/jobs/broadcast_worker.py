from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import select

from app.database.models.broadcast import Broadcast, BroadcastStatus
from app.database.session import session_scope
from app.services.broadcast_service import run_worker

logger = logging.getLogger(__name__)


async def resume_interrupted_broadcasts(bot: Bot, database_url: str, sessionmaker) -> None:
    """Runs once at boot. If the process died mid-broadcast, any Broadcast still marked
    RUNNING has PENDING deliveries left over — resume each one instead of losing progress."""
    async with session_scope(sessionmaker) as session:
        result = await session.execute(select(Broadcast.id).where(Broadcast.status == BroadcastStatus.RUNNING))
        ids = [row[0] for row in result.all()]

    for broadcast_id in ids:
        logger.info("Resuming interrupted broadcast %d", broadcast_id)
        await run_worker(bot, database_url, broadcast_id)
