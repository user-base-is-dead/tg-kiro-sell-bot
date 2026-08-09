from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models.support import SupportTicket, TicketMessage, TicketStatus
from app.database.models.user import User
from app.jobs.ticket_archival import IDLE_AUTO_CLOSE_AFTER, archive_stale_tickets

NOW = datetime.now(UTC)


async def _seed(sessionmaker, *, opened_ago: timedelta, message_ages: list[timedelta], status=TicketStatus.OPEN) -> int:
    async with sessionmaker() as session:
        user = User(telegram_id=777, username="u", referral_code="R1", locale="en", chat_id=777)
        session.add(user)
        await session.flush()

        ticket = SupportTicket(
            ticket_number=f"T-{opened_ago.total_seconds():.0f}-{len(message_ages)}",
            user_id=user.id,
            category="General",
            subject="s",
            status=status,
            opened_at=NOW - opened_ago,
        )
        session.add(ticket)
        await session.flush()

        for age in message_ages:
            session.add(
                TicketMessage(
                    ticket_id=ticket.id,
                    author_type="USER",
                    author_telegram_id=777,
                    content="x",
                    created_at=NOW - age,
                )
            )
        await session.commit()
        return ticket.id


async def _status(sessionmaker, ticket_id: int) -> TicketStatus:
    async with sessionmaker() as session:
        return (await session.get(SupportTicket, ticket_id)).status


async def test_ticket_idle_for_more_than_a_day_is_closed(sqlite_sessionmaker) -> None:
    tid = await _seed(sqlite_sessionmaker, opened_ago=timedelta(days=5), message_ages=[timedelta(hours=30)])
    assert await archive_stale_tickets(sqlite_sessionmaker) == 1
    assert await _status(sqlite_sessionmaker, tid) == TicketStatus.CLOSED


async def test_recent_reply_keeps_an_old_ticket_open(sqlite_sessionmaker) -> None:
    """The regression this exists for: closing was keyed off `opened_at`, so a conversation
    somebody had replied to minutes ago was force-closed purely for being old."""
    tid = await _seed(
        sqlite_sessionmaker,
        opened_ago=timedelta(days=30),
        message_ages=[timedelta(days=30), timedelta(minutes=1)],
    )
    assert await archive_stale_tickets(sqlite_sessionmaker) == 0
    assert await _status(sqlite_sessionmaker, tid) == TicketStatus.OPEN


async def test_ticket_with_no_messages_ages_out_from_opened_at(sqlite_sessionmaker) -> None:
    tid = await _seed(sqlite_sessionmaker, opened_ago=timedelta(days=2), message_ages=[])
    assert await archive_stale_tickets(sqlite_sessionmaker) == 1
    assert await _status(sqlite_sessionmaker, tid) == TicketStatus.CLOSED


async def test_just_inside_the_deadline_survives(sqlite_sessionmaker) -> None:
    tid = await _seed(
        sqlite_sessionmaker, opened_ago=timedelta(days=9), message_ages=[IDLE_AUTO_CLOSE_AFTER - timedelta(minutes=5)]
    )
    assert await archive_stale_tickets(sqlite_sessionmaker) == 0
    assert await _status(sqlite_sessionmaker, tid) == TicketStatus.OPEN


@pytest.mark.parametrize("status", [TicketStatus.OPEN, TicketStatus.PENDING, TicketStatus.RESOLVED])
async def test_every_live_status_is_swept(sqlite_sessionmaker, status: TicketStatus) -> None:
    tid = await _seed(
        sqlite_sessionmaker, opened_ago=timedelta(days=3), message_ages=[timedelta(days=2)], status=status
    )
    assert await archive_stale_tickets(sqlite_sessionmaker) == 1
    assert await _status(sqlite_sessionmaker, tid) == TicketStatus.CLOSED


async def test_already_closed_tickets_are_left_alone(sqlite_sessionmaker) -> None:
    await _seed(
        sqlite_sessionmaker,
        opened_ago=timedelta(days=90),
        message_ages=[timedelta(days=90)],
        status=TicketStatus.CLOSED,
    )
    assert await archive_stale_tickets(sqlite_sessionmaker) == 0


async def test_the_deadline_is_24_hours() -> None:
    assert IDLE_AUTO_CLOSE_AFTER == timedelta(hours=24)
