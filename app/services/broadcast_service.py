from __future__ import annotations

import asyncio
import json
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


async def create_broadcast(
    session: AsyncSession,
    *,
    admin_id: int,
    title: str,
    body: str,
    parts: list[dict] | None = None,
) -> Broadcast:
    """`parts` are {"chat_id", "message_id"} coordinates of the messages the admin composed. When
    given, delivery copies those messages; when omitted, `body` is sent as plain text."""
    result = await session.execute(select(User).where(User.status == UserStatus.ACTIVE, User.chat_id.is_not(None)))
    targets = list(result.scalars().all())

    broadcast = Broadcast(
        created_by_admin_id=admin_id,
        title=title,
        body=body,
        parts_json=json.dumps(parts) if parts else None,
        status=BroadcastStatus.RUNNING,
        total_targets=len(targets),
        started_at=datetime.now(UTC),
    )
    session.add(broadcast)
    await session.flush()

    session.add_all([BroadcastDelivery(broadcast_id=broadcast.id, user_id=u.id, status=DeliveryStatus.PENDING) for u in targets])
    await session.flush()
    return broadcast


async def _deliver(bot: Bot, chat_id: int, broadcast: Broadcast, parts: list[dict] | None) -> None:
    """Send one broadcast to one user.

    `copy_message` is what makes "send whatever you composed" work: it reproduces the admin's
    original message — text, photo, video, audio, document, voice, sticker, album item — without a
    "forwarded from" header and without re-uploading the file. Each part is copied in order, so the
    user receives the same sequence the admin wrote.
    """
    if not parts:
        await bot.send_message(chat_id, broadcast.body)
        return

    for part in parts:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=part["chat_id"],
            message_id=part["message_id"],
        )


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

                parts = json.loads(broadcast.parts_json) if broadcast.parts_json else None

                for delivery, target_user in batch:
                    try:
                        await _deliver(bot, target_user.chat_id, broadcast, parts)
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
