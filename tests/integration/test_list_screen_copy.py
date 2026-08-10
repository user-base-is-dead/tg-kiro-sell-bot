"""My Orders and My Tickets used to be a bare title over a column of buttons.

A bubble and its inline keyboard share one width and Telegram takes the wider of the two, so a
two-word title squeezed the whole button column into a narrow strip — the same defect the product
detail screen was padded to fix. These screens don't need a pad: they need to say what they are.
The width assertions here are the guard, so the description can't be trimmed back to a title
without the buttons quietly going narrow again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.bot.handlers.orders.history import _render_detail
from app.bot.handlers.support.my_tickets import render_ticket_card
from app.database.models.order import Order, OrderItem, OrderStatus
from app.locales.i18n import t

# Comfortably past the ~23-char titles that caused the squeeze, and past the widest button label a
# row can carry ("🟢 ORD-93D1E3 — $5.00" and "🟢 TCK-3F5108 — Hello").
MIN_WIDTH = 45


def _widest_line(text: str) -> int:
    return max(len(line) for line in text.split("\n"))


@pytest.mark.parametrize(
    "key",
    ["orders.history_title", "orders.empty_history", "support.tickets_title", "support.no_tickets"],
)
def test_list_screens_are_wider_than_their_button_column(key: str) -> None:
    text = t(key, "en")
    assert _widest_line(text) >= MIN_WIDTH, f"{key} is too narrow to hold the buttons open"
    # A title alone is what the bug looked like; every one of these must actually explain itself.
    assert len(text.split("\n")) >= 3, f"{key} has no description under its heading"


@pytest.mark.asyncio
async def test_the_order_detail_card_explains_the_order(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        order = Order(
            order_number="ORD-93D1E3",
            user_id=1,
            status=OrderStatus.COMPLETED,
            subtotal_minor=500,
            discount_minor=0,
            total_minor=500,
            currency="USD",
            idempotency_key="k1",
            placed_at=datetime(2026, 8, 10, 11, 0, tzinfo=UTC),
        )
        session.add(order)
        await session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=None,
                product_name="Kiro Pro",
                unit_price_minor=500,
                qty=1,
                warranty_days=1,
            )
        )
        await session.commit()

        text, _ = await _render_detail(session, order.id, "en")

    assert "🟢" in text, "the status badge matches the one on the list row"
    assert "ORD-93D1E3" in text
    assert "10 Aug 2026, 11:00 UTC" in text
    assert "Kiro Pro" in text
    assert "$5.00" in text
    assert _widest_line(text) >= MIN_WIDTH


def _ticket(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        ticket_number="TCK-3F5108",
        status=SimpleNamespace(value=status),
        category="General",
        opened_at=datetime(2026, 8, 10, 11, 0, tzinfo=UTC),
    )


def _messages() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(author_type="USER", content="Hello"),
        SimpleNamespace(author_type="STAFF", content="How can we help?"),
    ]


def test_an_open_ticket_card_invites_a_reply() -> None:
    text = render_ticket_card(_ticket("OPEN"), _messages(), "en")

    assert "TCK-3F5108" in text
    assert "General" in text
    assert "10 Aug 2026, 11:00 UTC" in text
    assert "<b>You:</b> Hello" in text
    assert "<b>Support:</b> How can we help?" in text
    assert t("support.ticket_hint_open", "en") in text
    assert _widest_line(text) >= MIN_WIDTH


def test_a_closed_ticket_card_says_replies_go_nowhere() -> None:
    """The screenshot case: a CLOSED ticket looked exactly like an open one, so there was nothing
    telling the customer their next message would land in a thread nobody reads."""
    text = render_ticket_card(_ticket("CLOSED"), _messages(), "en")

    assert t("support.ticket_hint_closed", "en") in text
    assert t("support.ticket_hint_open", "en") not in text


def test_ticket_content_is_escaped_not_injected() -> None:
    """A message containing markup would otherwise make Telegram reject the whole card, and the
    ticket would simply stop opening."""
    messages = [SimpleNamespace(author_type="USER", content="<b>hi</b> & bye")]
    text = render_ticket_card(_ticket("OPEN"), messages, "en")

    assert "&lt;b&gt;hi&lt;/b&gt; &amp; bye" in text


def test_a_message_with_no_content_does_not_break_the_card() -> None:
    """`TicketMessage.content` is nullable — an attachment-only message has none."""
    messages = [SimpleNamespace(author_type="USER", content=None)]
    assert "<b>You:</b>" in render_ticket_card(_ticket("OPEN"), messages, "en")
