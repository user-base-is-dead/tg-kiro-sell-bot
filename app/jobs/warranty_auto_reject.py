from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.repositories.user_repo import UserRepo
from app.database.repositories.warranty_repo import WarrantyRepo
from app.database.session import session_scope
from app.services.support_service import close_claim_ticket
from app.services.warranty_service import format_duration, now_utc, reject_claim
from app.utils.time import as_utc

logger = logging.getLogger(__name__)

REASON = "Auto-closed: no response from our team within the review window."


async def auto_reject_expired_warranty_claims(sessionmaker: async_sessionmaker, bot: Bot) -> int:
    """Close claims that blew through their staff-response deadline.

    Driven entirely off the stored `claim_deadline_at`, so a run after any amount of downtime
    catches everything that lapsed while the process was gone, exactly once. Closing a claim this
    way follows the same rule as a manual /reject: the warranty falls back to its original
    timeline, neither restarted nor extended.
    """
    async with session_scope(sessionmaker) as session:
        now = now_utc()
        repo = WarrantyRepo(session)
        user_repo = UserRepo(session)
        stale = await repo.list_claims_past_deadline(now)

        for warranty in stale:
            outcome = reject_claim(warranty, reason=REASON, at=now)
            await session.flush()
            await close_claim_ticket(
                bot, session, ticket_id=warranty.claim_ticket_id, reason=f"Warranty claim #{warranty.id} auto-closed"
            )

            if outcome.granted_seconds > 0:
                tail = (
                    f"⏱️ Your warranty is still active with <b>{format_duration(outcome.granted_seconds)}</b> "
                    f"remaining (expires {as_utc(warranty.expires_at):%d %b %Y %H:%M} UTC)."
                )
            else:
                tail = "The warranty period for this item has also ended."

            text = (
                "❌ Your warranty claim was <b>closed automatically</b> because our team didn't "
                "respond in time.\n\n"
                f"{tail}\n\n"
                "We're sorry about that — please contact Customer Support and we'll pick it up."
            )

            owner = await user_repo.get_by_id(warranty.user_id)
            if owner is not None:
                try:
                    await bot.send_message(owner.telegram_id, text)
                except TelegramAPIError as exc:
                    logger.warning("Auto-reject notice to %s failed (%s)", owner.telegram_id, exc)

            logger.info("Auto-rejected warranty claim %s", warranty.id)

        return len(stale)
