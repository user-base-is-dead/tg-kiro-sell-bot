from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.broadcast import Broadcast, BroadcastDelivery, BroadcastStatus, DeliveryStatus
from app.database.models.user import User, UserStatus
from app.database.session import build_engine, build_sessionmaker, session_scope

logger = logging.getLogger(__name__)

SEND_DELAY_SECONDS = 0.05  # ~20 msg/sec, well under Telegram's global cap


async def create_broadcast(session: AsyncSession, *, admin_id: int, title: str, body: str) -> Broadcast:
    result = await session.execute(select(User).where(User.status == UserStatus.ACTIVE, User.chat_id.is_not(None)))
    targets = list(result.scalars().all())

    broadcast = Broadcast(
        created_by_admin_id=admin_id,
        title=title,
        body=body,
        status=BroadcastStatus.RUNNING,
        total_targets=len(targets),
        started_at=datetime.now(UTC),
    )
    session.add(broadcast)
    await session.flush()

    session.add_all([BroadcastDelivery(broadcast_id=broadcast.id, user_id=u.id, status=DeliveryStatus.PENDING) for u in targets])
    await session.flush()
    return broadcast


async def run_worker(bot: Bot, database_url: str, broadcast_id: int) -> None:
    """Runs against its own short-lived engine (this is a background task outside any
    request-scoped session). Only ever touches PENDING deliveries, so re-running this after
    a crash/restart resumes exactly where it left off — nothing is double-sent."""
    engine = build_engine(database_url)
    sessionmaker = build_sessionmaker(engine)

    try:
        while True:
            async with session_scope(sessionmaker) as session:
                broadcast = await session.get(Broadcast, broadcast_id)
                if broadcast is None:
                    return

                result = await session.execute(
                    select(BroadcastDelivery, User)
                    .join(User, User.id == BroadcastDelivery.user_id)
                    .where(BroadcastDelivery.broadcast_id == broadcast_id, BroadcastDelivery.status == DeliveryStatus.PENDING)
                    .limit(25)
                )
                batch = result.all()

                if not batch:
                    broadcast.status = BroadcastStatus.COMPLETED
                    broadcast.finished_at = datetime.now(UTC)
                    await session.flush()
                    return

                for delivery, target_user in batch:
                    try:
                        await bot.send_message(target_user.chat_id, broadcast.body)
                        delivery.status = DeliveryStatus.SENT
                        broadcast.sent_count += 1
                    except TelegramForbiddenError:
                        delivery.status = DeliveryStatus.BLOCKED
                        broadcast.failed_count += 1
                    except TelegramRetryAfter as exc:
                        await asyncio.sleep(exc.retry_after)
                        continue
                    except Exception as exc:  # noqa: BLE001 — best-effort delivery, never abort the batch
                        delivery.status = DeliveryStatus.FAILED
                        delivery.error = str(exc)[:512]
                        broadcast.failed_count += 1
                    await asyncio.sleep(SEND_DELAY_SECONDS)

                await session.flush()
    finally:
        await engine.dispose()
