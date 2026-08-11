"""A warranty claim's ticket dies with the claim.

The bug: filing a claim opens a support ticket + forum topic, but `/done`, `/reject` and the
auto-reject job only touched the warranty row. The thread stayed OPEN, so staff had to remember a
separate `/close` per claim and the customer could keep writing into a decision already made.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.bot.handlers.admin import warranty_claims
from app.database.models.order import Order, OrderItem, OrderStatus, Warranty, WarrantyStatus
from app.database.models.support import SupportTicket, TicketStatus
from app.database.repositories.user_repo import UserRepo
from app.jobs.warranty_auto_reject import auto_reject_expired_warranty_claims
from app.services.warranty_service import CLAIM_GRACE, open_claim

NOW = datetime.now(UTC)
TOPIC_ID = 4242


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.closed_topics: list[tuple[int, int]] = []

    async def send_message(self, chat_id: int, text: str, **_kw: object):
        self.sent.append((chat_id, text))

    async def close_forum_topic(self, chat_id: int, message_thread_id: int):
        self.closed_topics.append((chat_id, message_thread_id))


class _FakeMessage:
    def __init__(self, text: str, bot: _FakeBot) -> None:
        self.text = text
        self.bot = bot
        self.answers: list[str] = []

    async def answer(self, text: str, **_kw: object):
        self.answers.append(text)


class _Admin:
    telegram_id = 999


@pytest.fixture
def support_group(monkeypatch) -> int:
    """`close_forum_topic` is only attempted when a support group is configured."""
    from app.core import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "support_group_id", -100999, raising=False)
    return -100999


async def _claimed_warranty(sessionmaker, *, telegram_id: int, claimed_at: datetime) -> tuple[int, int]:
    """A CLAIMED warranty wired to an OPEN ticket with a forum topic, exactly as claim.py leaves it."""
    async with sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
            chat_id=telegram_id, default_locale="en",
        )
        order = Order(
            order_number=f"ORD-{telegram_id}", user_id=user.id, status=OrderStatus.COMPLETED,
            subtotal_minor=0, discount_minor=0, total_minor=0, currency="USD",
            idempotency_key=f"k{telegram_id}", placed_at=NOW - timedelta(days=1),
        )
        session.add(order)
        await session.flush()
        item = OrderItem(
            order_id=order.id, product_id=None, product_name="Kiro Pro",
            unit_price_minor=0, qty=1, warranty_days=30,
        )
        session.add(item)
        ticket = SupportTicket(
            ticket_number=f"TCK-{telegram_id}", user_id=user.id, category="Warranty Claim",
            subject="s", status=TicketStatus.OPEN, topic_id=TOPIC_ID, opened_at=claimed_at,
        )
        session.add_all([item, ticket])
        await session.flush()
        warranty = Warranty(
            order_item_id=item.id, user_id=user.id, starts_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=29), status=WarrantyStatus.ACTIVE,
        )
        session.add(warranty)
        await session.flush()
        open_claim(warranty, ticket_id=ticket.id, at=claimed_at)
        await session.commit()
        return warranty.id, ticket.id


async def _ticket(sessionmaker, ticket_id: int) -> SupportTicket:
    async with sessionmaker() as session:
        return await session.get(SupportTicket, ticket_id)


@pytest.mark.asyncio
async def test_reject_closes_the_claim_ticket_and_its_topic(sqlite_sessionmaker, support_group) -> None:
    warranty_id, ticket_id = await _claimed_warranty(sqlite_sessionmaker, telegram_id=8201, claimed_at=NOW)
    bot = _FakeBot()

    async with sqlite_sessionmaker() as session:
        msg = _FakeMessage(f"/reject {warranty_id} not covered", bot)
        await warranty_claims.reject_warranty_claim(msg, session, _Admin())
        await session.commit()

    ticket = await _ticket(sqlite_sessionmaker, ticket_id)
    assert ticket.status is TicketStatus.CLOSED
    assert ticket.closed_at is not None
    assert bot.closed_topics == [(support_group, TOPIC_ID)]


@pytest.mark.asyncio
async def test_done_closes_the_claim_ticket_and_its_topic(sqlite_sessionmaker, support_group) -> None:
    warranty_id, ticket_id = await _claimed_warranty(sqlite_sessionmaker, telegram_id=8202, claimed_at=NOW)
    bot = _FakeBot()

    async with sqlite_sessionmaker() as session:
        msg = _FakeMessage(f"/done {warranty_id}", bot)
        await warranty_claims.approve_warranty_claim(msg, session, _Admin())
        await session.commit()

    assert (await _ticket(sqlite_sessionmaker, ticket_id)).status is TicketStatus.CLOSED
    assert bot.closed_topics == [(support_group, TOPIC_ID)]


@pytest.mark.asyncio
async def test_auto_reject_closes_the_claim_ticket_too(sqlite_sessionmaker, support_group) -> None:
    """The timeout path ends the conversation just as firmly as an admin's /reject."""
    _, ticket_id = await _claimed_warranty(
        sqlite_sessionmaker, telegram_id=8203, claimed_at=NOW - CLAIM_GRACE - timedelta(hours=1)
    )
    bot = _FakeBot()

    assert await auto_reject_expired_warranty_claims(sqlite_sessionmaker, bot) == 1

    assert (await _ticket(sqlite_sessionmaker, ticket_id)).status is TicketStatus.CLOSED
    assert bot.closed_topics == [(support_group, TOPIC_ID)]


@pytest.mark.asyncio
async def test_a_claim_with_no_ticket_still_resolves(sqlite_sessionmaker, support_group) -> None:
    """`claim_ticket_id` is nullable — resolving must not blow up when nothing is linked."""
    warranty_id, ticket_id = await _claimed_warranty(sqlite_sessionmaker, telegram_id=8204, claimed_at=NOW)
    bot = _FakeBot()

    async with sqlite_sessionmaker() as session:
        warranty = await session.get(Warranty, warranty_id)
        warranty.claim_ticket_id = None
        await session.commit()

    async with sqlite_sessionmaker() as session:
        msg = _FakeMessage(f"/reject {warranty_id} nope", bot)
        await warranty_claims.reject_warranty_claim(msg, session, _Admin())
        await session.commit()

    assert (await _ticket(sqlite_sessionmaker, ticket_id)).status is TicketStatus.OPEN
    assert bot.closed_topics == []
