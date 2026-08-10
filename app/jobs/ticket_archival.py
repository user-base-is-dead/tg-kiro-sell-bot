from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models.support import SupportTicket, TicketMessage, TicketStatus
from app.database.session import session_scope
from app.services.support_service import announce_closure
from app.utils.time import as_utc

logger = logging.getLogger(__name__)

IDLE_AUTO_CLOSE_AFTER = timedelta(hours=24)

_LIVE_STATUSES = (TicketStatus.OPEN, TicketStatus.PENDING, TicketStatus.RESOLVED)


async def archive_stale_tickets(sessionmaker: async_sessionmaker, bot: Bot | None = None) -> int:
    """Close tickets that have gone quiet for a day.

    Idleness is measured from the last message on the ticket, not from `opened_at` as this
    previously did — that closed threads by age alone, so a busy conversation was force-closed on
    its deadline no matter that someone had written a minute earlier."""
    async with session_scope(sessionmaker) as session:
        now = datetime.now(UTC)
        cutoff = now - IDLE_AUTO_CLOSE_AFTER

        last_message = (
            select(TicketMessage.ticket_id, func.max(TicketMessage.created_at).label("last_at"))
            .group_by(TicketMessage.ticket_id)
            .subquery()
        )
        result = await session.execute(
            select(SupportTicket, last_message.c.last_at)
            .outerjoin(last_message, last_message.c.ticket_id == SupportTicket.id)
            .where(SupportTicket.status.in_(_LIVE_STATUSES))
        )

        closed: list[SupportTicket] = []
        for ticket, last_at in result.all():
            # A ticket with no messages at all falls back to when it was opened, so it can still
            # age out instead of living forever.
            if as_utc(last_at or ticket.opened_at) >= cutoff:
                continue
            ticket.status = TicketStatus.CLOSED
            ticket.closed_at = now
            ticket.close_reason = "Auto-closed: no activity for 24 hours"
            closed.append(ticket)

        if not closed:
            return 0

        await session.flush()
        if bot is not None:
            for ticket in closed:
                await announce_closure(bot, session, ticket)

        logger.info("Auto-closed %d idle tickets", len(closed))
        return len(closed)
