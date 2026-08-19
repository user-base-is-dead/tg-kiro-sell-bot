"""The order thread as a dispute channel: delivered closes itself, cancelled stays open and connected.

These tests drive the real services against SQLite with a fake Bot, so what is asserted is what
Telegram would actually be asked to do — which topic was closed, which chat a relayed message went
to — rather than an internal flag standing in for it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.database.models.order import Order, OrderStatus, RefundState
from app.database.models.support import SupportTicket, TicketMessage, TicketStatus
from app.database.models.user import User
from app.database.repositories.support_repo import SupportRepo
from app.jobs.ticket_archival import archive_stale_tickets
from app.services import order_thread_service, refund_service, support_service

ORDERS_GROUP = -1009999
SUPPORT_GROUP = -1001111
TOPIC = 4242


@dataclass
class FakeTopic:
    message_thread_id: int


@dataclass
class FakeBot:
    """Records what would have gone to Telegram. `sent` is (chat_id, text, thread_id) per message."""

    sent: list[tuple[int, str, int | None]] = field(default_factory=list)
    created_topics: list[tuple[int, str]] = field(default_factory=list)
    closed: list[tuple[int, int]] = field(default_factory=list)
    reopened: list[tuple[int, int]] = field(default_factory=list)
    next_topic_id: int = TOPIC

    async def create_forum_topic(self, chat_id: int, name: str) -> FakeTopic:
        self.created_topics.append((chat_id, name))
        return FakeTopic(self.next_topic_id)

    async def send_message(self, chat_id: int, text: str, message_thread_id: int | None = None, **kw) -> None:
        self.sent.append((chat_id, text, message_thread_id))

    async def close_forum_topic(self, chat_id: int, message_thread_id: int) -> None:
        self.closed.append((chat_id, message_thread_id))

    async def reopen_forum_topic(self, chat_id: int, message_thread_id: int) -> None:
        self.reopened.append((chat_id, message_thread_id))


@pytest.fixture(autouse=True)
def _groups(monkeypatch):
    """Point the services at fake groups without touching the real .env-backed Settings cache."""

    @dataclass
    class S:
        orders_group_id: int | None = ORDERS_GROUP
        support_group_id: int | None = SUPPORT_GROUP
        default_currency: str = "USD"
        admin_ids: tuple = ()

    settings = S()
    for module in (order_thread_service, support_service, refund_service):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    return settings


async def _seed(sessionmaker, *, status: OrderStatus) -> tuple[int, str]:
    async with sessionmaker() as session:
        user = User(telegram_id=555, username="buyer", referral_code="R", locale="en", chat_id=555)
        session.add(user)
        await session.flush()
        order = Order(
            order_number="ORD-1",
            user_id=user.id,
            status=status,
            subtotal_minor=1000,
            total_minor=1000,
            currency="USD",
            idempotency_key=f"k-{status.value}",
            placed_at=datetime.now(UTC),
        )
        session.add(order)
        await session.commit()
        return user.id, order.id


async def test_delivered_order_closes_its_own_thread(sqlite_sessionmaker) -> None:
    _, order_id = await _seed(sqlite_sessionmaker, status=OrderStatus.COMPLETED)
    bot = FakeBot()

    async with sqlite_sessionmaker() as session:
        order = await session.get(Order, order_id)
        await order_thread_service.sync(bot, session, order)
        await session.commit()

    assert bot.created_topics == [(ORDERS_GROUP, "ORD-1 · @buyer")]
    assert bot.closed == [(ORDERS_GROUP, TOPIC)]


async def test_cancelled_order_becomes_a_dispute_the_buyer_is_connected_to(sqlite_sessionmaker) -> None:
    """The whole flow: the thread stays open, the buyer's own DM relays into it, and staff replies
    come back out — without the buyer ever opening a ticket."""
    user_id, order_id = await _seed(sqlite_sessionmaker, status=OrderStatus.CANCELLED)
    bot = FakeBot()

    async with sqlite_sessionmaker() as session:
        order = await session.get(Order, order_id)
        order.refund_state = RefundState.PARKED
        order.refund_amount_minor = 1000
        buyer = await session.get(User, user_id)

        thread = await refund_service.open_or_reuse_thread(
            bot,
            session,
            order=order,
            buyer=buyer,
            reason="Out of stock",
            refunded_minor=1000,
            refund_event=None,
        )
        await session.commit()
        ticket_id = thread.ticket.id

    assert thread.created and thread.reached_staff
    # The dispute lives in the ORDER thread, not in a fresh support-group ticket.
    assert bot.created_topics == [(ORDERS_GROUP, "ORD-1 · @buyer")]
    assert all(chat == ORDERS_GROUP for chat, _, _ in bot.sent)
    assert (ORDERS_GROUP, TOPIC) not in bot.closed

    async with sqlite_sessionmaker() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        assert ticket.group_chat_id == ORDERS_GROUP
        assert ticket.topic_id == TOPIC
        assert ticket.order_id == order_id
        assert ticket.status is TicketStatus.OPEN
        # It is the order's linked thread, so the buyer's Refunds screen points at the same place.
        assert (await session.get(Order, order_id)).refund_ticket_id == ticket_id

        # A later sync (any admin action on the order) must not close a live conversation.
        await order_thread_service.sync(bot, session, await session.get(Order, order_id))
        assert (ORDERS_GROUP, TOPIC) not in bot.closed

    # The buyer just types; no ticket is opened by them.
    async with sqlite_sessionmaker() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        buyer = await session.get(User, user_id)
        bot.sent.clear()
        await support_service.relay_user_message(bot, session, ticket=ticket, user=buyer, text="where is my money")
        await session.commit()

    assert bot.sent == [(ORDERS_GROUP, "where is my money", TOPIC)]


async def test_an_open_dispute_blocks_new_tickets_and_warranty_claims(sqlite_sessionmaker) -> None:
    user_id, order_id = await _seed(sqlite_sessionmaker, status=OrderStatus.CANCELLED)

    async with sqlite_sessionmaker() as session:
        session.add(
            SupportTicket(
                ticket_number="TCK-9",
                user_id=user_id,
                category=support_service.ORDER_DISPUTE_CATEGORY,
                subject="Order dispute — ORD-1",
                status=TicketStatus.OPEN,
                topic_id=TOPIC,
                group_chat_id=ORDERS_GROUP,
                order_id=order_id,
                opened_at=datetime.now(UTC),
            )
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        active = await support_service.active_thread(session, user_id)

    assert active is not None
    # "order" is what picks the locale line telling them to type here rather than open anything.
    assert active.kind == "order"
    assert active.reference == "TCK-9"


async def test_close_frees_the_normal_ticket_system_and_closes_the_thread(sqlite_sessionmaker) -> None:
    user_id, order_id = await _seed(sqlite_sessionmaker, status=OrderStatus.CANCELLED)
    bot = FakeBot()

    async with sqlite_sessionmaker() as session:
        order = await session.get(Order, order_id)
        order.refund_state = RefundState.PARKED
        buyer = await session.get(User, user_id)
        thread = await refund_service.open_or_reuse_thread(
            bot, session, order=order, buyer=buyer, reason="r", refunded_minor=1000, refund_event=None
        )
        await session.commit()
        ticket_id = thread.ticket.id

    async with sqlite_sessionmaker() as session:
        ticket = await support_service.close_ticket(session, ticket_id=ticket_id, reason="Closed by admin 1")
        await support_service.announce_closure(bot, session, ticket)
        await session.commit()

    # The order's own topic is what closes — the dispute was hosted there.
    assert (ORDERS_GROUP, TOPIC) in bot.closed
    # And the buyer is told in dispute-specific words, in their DM.
    dm = [text for chat, text, _ in bot.sent if chat == 555]
    assert dm and "Live Chat is open to you again" in dm[-1]

    async with sqlite_sessionmaker() as session:
        assert await support_service.active_thread(session, user_id) is None
        assert not await order_thread_service.dispute_is_open(session, await session.get(Order, order_id))

    # With the dispute closed, the next sync retires the thread like any other dead order.
    async with sqlite_sessionmaker() as session:
        bot.closed.clear()
        order = await session.get(Order, order_id)
        order.refund_state = RefundState.SETTLED
        await order_thread_service.sync(bot, session, order)
        await session.commit()
    assert bot.closed == [(ORDERS_GROUP, TOPIC)]


async def test_a_dispute_is_never_auto_closed_by_the_idle_sweep(sqlite_sessionmaker) -> None:
    """A refund still parked must not leave the queue because nobody typed for a day."""
    user_id, order_id = await _seed(sqlite_sessionmaker, status=OrderStatus.CANCELLED)
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)

    async with sqlite_sessionmaker() as session:
        dispute = SupportTicket(
            ticket_number="TCK-D",
            user_id=user_id,
            category=support_service.ORDER_DISPUTE_CATEGORY,
            subject="s",
            status=TicketStatus.OPEN,
            order_id=order_id,
            opened_at=long_ago,
        )
        plain = SupportTicket(
            ticket_number="TCK-P",
            user_id=user_id,
            category="General",
            subject="s",
            status=TicketStatus.OPEN,
            opened_at=long_ago,
        )
        session.add_all([dispute, plain])
        await session.flush()
        session.add(
            TicketMessage(
                ticket_id=dispute.id,
                author_type="USER",
                author_telegram_id=555,
                content="x",
                created_at=long_ago,
            )
        )
        await session.commit()
        dispute_id, plain_id = dispute.id, plain.id

    assert await archive_stale_tickets(sqlite_sessionmaker) == 1

    async with sqlite_sessionmaker() as session:
        assert (await session.get(SupportTicket, dispute_id)).status is TicketStatus.OPEN
        assert (await session.get(SupportTicket, plain_id)).status is TicketStatus.CLOSED


async def test_topic_numbers_are_resolved_per_group(sqlite_sessionmaker) -> None:
    """Telegram numbers topics per chat, so the same id means two different conversations in two
    groups. Resolving on the id alone would relay a staff reply to the wrong buyer."""
    user_id, order_id = await _seed(sqlite_sessionmaker, status=OrderStatus.CANCELLED)

    async with sqlite_sessionmaker() as session:
        session.add_all(
            [
                SupportTicket(
                    ticket_number="TCK-SUP",
                    user_id=user_id,
                    category="General",
                    subject="s",
                    status=TicketStatus.OPEN,
                    topic_id=TOPIC,
                    opened_at=datetime.now(UTC),
                ),
                SupportTicket(
                    ticket_number="TCK-ORD",
                    user_id=user_id,
                    category=support_service.ORDER_DISPUTE_CATEGORY,
                    subject="s",
                    status=TicketStatus.OPEN,
                    topic_id=TOPIC,
                    group_chat_id=ORDERS_GROUP,
                    order_id=order_id,
                    opened_at=datetime.now(UTC),
                ),
            ]
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        repo = SupportRepo(session)
        in_orders = await repo.get_by_topic_in_group(TOPIC, ORDERS_GROUP, is_support_group=False)
        in_support = await repo.get_by_topic_in_group(TOPIC, SUPPORT_GROUP, is_support_group=True)

    assert in_orders.ticket_number == "TCK-ORD"
    # NULL group_chat_id has always meant the support group, so legacy rows still resolve there.
    assert in_support.ticket_number == "TCK-SUP"
