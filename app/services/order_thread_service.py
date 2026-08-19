from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.models.order import FundingSource, Order, OrderStatus, RefundState
from app.database.models.order_event import OrderEvent, OrderEventKind
from app.database.models.support import SupportTicket, TicketStatus
from app.database.models.user import User
from app.database.repositories.order_event_repo import OrderEventRepo
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.support_repo import SupportRepo
from app.database.repositories.user_repo import UserRepo
from app.utils.money import format_minor
from app.utils.text import escape_html
from app.utils.time import as_utc

logger = logging.getLogger(__name__)

# One line per event kind, written for somebody scrolling the group rather than reading a table.
_EVENT_LINE = {
    OrderEventKind.PLACED: "🛒 <b>Placed</b>",
    OrderEventKind.DELIVERED: "📬 <b>Delivered</b>",
    OrderEventKind.DECLINED: "🚫 <b>Declined</b>",
    OrderEventKind.REFUND_PARKED: "💰 <b>Refund parked</b>",
    OrderEventKind.REFUND_PAID_OUT: "📤 <b>Refund sent</b>",
    OrderEventKind.REFUND_MOVED: "➡️ <b>Refund moved to wallet</b>",
    OrderEventKind.TICKET_OPENED: "🎫 <b>Refund ticket</b>",
}

_STATUS_EMOJI = {
    "PENDING": "🟡",
    "PROCESSING": "🔵",
    "COMPLETED": "🟢",
    "CANCELLED": "🔴",
    "FAILED": "⚠️",
}


def topic_name(order: Order, who: str) -> str:
    """Telegram caps topic names at 128 chars. The order number leads because it is what staff quote
    and search on; the buyer is the human-readable half."""
    return f"{order.order_number} · {who}"[:128]


async def _buyer_handle(session: AsyncSession, order: Order) -> str:
    buyer = await UserRepo(session).get_by_id(order.user_id)
    if buyer is None:
        return f"user {order.user_id}"
    return f"@{buyer.username}" if buyer.username else f"id {buyer.telegram_id}"


async def _order_card(session: AsyncSession, order: Order) -> str:
    """The opening message of the thread: everything true about the order at a glance, so staff never
    have to leave the group to know what they are looking at."""
    buyer = await UserRepo(session).get_by_id(order.user_id)
    full = await OrderRepo(session).get_by_id(order.id) or order

    who = "—"
    if buyer is not None:
        handle = f"@{escape_html(buyer.username)}" if buyer.username else "no username"
        who = f"{handle} · <code>{buyer.telegram_id}</code>"

    lines = [
        f"{_STATUS_EMOJI.get(order.status.value, '•')} <b>{order.order_number}</b>",
        "",
        f"👤 {who}",
    ]
    if order.placed_at:
        lines.append(f"🕒 {as_utc(order.placed_at):%d %b %Y, %H:%M} UTC")

    lines.append("")
    for item in full.items:
        lines.append(
            f"• {escape_html(item.product_name)} ×{item.qty} — "
            f"{format_minor(item.unit_price_minor, order.currency)}"
        )

    paid = "💎 Crypto (USDT)" if order.funding_source is FundingSource.CRYPTO else "💳 Wallet"
    lines += ["", f"💰 <b>{format_minor(order.total_minor, order.currency)}</b> · {paid}"]
    lines += ["", f"🆔 <code>{order.id}</code>"]
    return "\n".join(lines)


def _event_line(event: OrderEvent, order: Order) -> str:
    head = _EVENT_LINE.get(event.kind, event.kind.value)
    parts = [f"{head} · <code>{event.event_number}</code>"]
    if event.amount_minor:
        parts.append(f"💵 {format_minor(event.amount_minor, event.currency or order.currency)}")
    if event.actor_telegram_id:
        parts.append(f"👮 <code>{event.actor_telegram_id}</code>")
    line = "\n".join(parts)
    if event.reason:
        line += f"\n{escape_html(event.reason)}"
    if event.reference:
        line += f"\n<code>{escape_html(event.reference)}</code>"
    line += f"\n<i>{as_utc(event.created_at):%d %b %H:%M} UTC</i>"
    return line


async def sync(bot: Bot, session: AsyncSession, order: Order) -> None:
    """Open this order's topic if it has none, then post whatever history it hasn't posted yet.

    One call after any action on an order is enough — it works out what is missing by comparing
    `order_events` against the high-water mark, so callers never have to say what happened. That also
    makes it self-healing: a post that failed while the group was unreachable goes out on the next
    action instead of being lost.

    Entirely best-effort. An order is committed to the database and visible in the admin panel
    regardless, so a misconfigured group, a deleted topic or a revoked bot must never fail a purchase
    or a refund. Every failure is logged and swallowed.
    """
    group_id = get_settings().orders_group_id
    if group_id is None:
        return

    try:
        if order.thread_id is None:
            who = await _buyer_handle(session, order)
            topic = await bot.create_forum_topic(chat_id=group_id, name=topic_name(order, who))
            order.thread_id = topic.message_thread_id
            await session.flush()
            await bot.send_message(
                group_id, await _order_card(session, order), message_thread_id=order.thread_id
            )

        events = await OrderEventRepo(session).list_for_order(order.id)
        pending = [e for e in events if e.id > (order.thread_last_event_id or 0)]
        for event in pending:
            await bot.send_message(
                group_id, _event_line(event, order), message_thread_id=order.thread_id
            )
            # Advanced per event, not once at the end: if the fifth post fails, the four already sent
            # are not sent again next time.
            order.thread_last_event_id = event.id
            await session.flush()

        # A dead order's thread is closed so the group reads as a queue of live work. Two
        # exceptions, both of them unfinished business: money still parked in the Refund Wallet, and
        # a dispute the buyer is connected to — that thread is the conversation now, and closing it
        # would mute both sides mid-sentence. Only an admin's /close ends it.
        if await dispute_is_open(session, order):
            return
        if order.status in (OrderStatus.CANCELLED, OrderStatus.FAILED) and order.refund_state is not RefundState.PARKED:
            await _close(bot, group_id, order)
        elif order.status is OrderStatus.COMPLETED:
            # Delivered is done: the topic closes itself, which is the whole reason the group stays
            # readable as work rather than as history.
            await _close(bot, group_id, order)

    except TelegramAPIError as exc:
        logger.warning("Couldn't update the order thread for %s (%s)", order.order_number, exc)
    except Exception as exc:  # noqa: BLE001 — never let the log take down the action it describes
        logger.error("Unexpected failure updating the order thread for %s: %s", order.order_number, exc)


async def _close(bot: Bot, group_id: int, order: Order) -> None:
    try:
        await bot.close_forum_topic(chat_id=group_id, message_thread_id=order.thread_id)
    except TelegramAPIError:
        # Already closed, or the topic is gone. Neither is worth a warning — this is cosmetic.
        pass


async def reopen(bot: Bot, session: AsyncSession, order: Order) -> None:
    """Bring a closed thread back before posting into it.

    A completed order's topic is closed, and Telegram refuses messages into a closed topic. Something
    can still happen afterwards — a delivered order gets declined and refunded — and that must land in
    the same thread rather than silently going nowhere.
    """
    group_id = get_settings().orders_group_id
    if group_id is None or order.thread_id is None:
        return
    try:
        await bot.reopen_forum_topic(chat_id=group_id, message_thread_id=order.thread_id)
    except TelegramAPIError:
        pass
    await sync(bot, session, order)


async def dispute_is_open(session: AsyncSession, order: Order) -> bool:
    """Is this order's thread currently a live conversation with the buyer?

    Read off the ticket rather than off the order's status, because the two answer different
    questions: the order is cancelled either way, and what decides whether the thread stays open is
    whether anybody is still talking in it.
    """
    if order.refund_ticket_id is None:
        return False
    ticket = await SupportRepo(session).get_by_id(order.refund_ticket_id)
    if ticket is None or ticket.order_id != order.id:
        return False
    return ticket.status in (TicketStatus.OPEN, TicketStatus.PENDING)


@dataclass(frozen=True)
class Dispute:
    """A dispute now running in the order's own topic. `reached_staff` is false when the group
    refused the post — the ticket exists, but nobody was told, so the buyer must not be invited to
    start typing into it."""

    ticket: SupportTicket
    reached_staff: bool


async def open_dispute(
    bot: Bot, session: AsyncSession, *, order: Order, buyer: User, body: str
) -> Dispute | None:
    """Turn this order's log thread into a conversation the buyer is connected to.

    Called when an order is cancelled or refunded. The thread already holds everything staff need —
    what was bought, what was paid, why it was declined, how much is parked — so the refund is
    settled there instead of in a fresh support ticket that repeats none of it. It stays open (see
    `sync`) until an admin runs /close.

    Returns None when there is no thread to host it (no ORDERS_GROUP_ID, or the group refused to
    open a topic), which is the caller's signal to fall back to an ordinary support ticket. Never
    raises: a refund that is already parked must not be undone by a misconfigured group.
    """
    from app.services import support_service

    group_id = get_settings().orders_group_id
    if group_id is None:
        return None

    try:
        # Brings the thread into existence if this order never had one, and catches its log up so the
        # buyer's first message does not arrive above the decline that prompted it. Reopen first: the
        # order may have been delivered, which closed the topic.
        if order.thread_id is not None:
            try:
                await bot.reopen_forum_topic(chat_id=group_id, message_thread_id=order.thread_id)
            except TelegramAPIError:
                pass
        await sync(bot, session, order)
        if order.thread_id is None:
            return None
        # `sync` closes the topic of a dead order, and at this point there is no dispute ticket yet
        # for it to know better — so it is reopened here, before anybody is told to type into it.
        try:
            await bot.reopen_forum_topic(chat_id=group_id, message_thread_id=order.thread_id)
        except TelegramAPIError:
            pass

        ticket, reached_staff = await support_service.create_order_dispute_ticket(
            bot,
            session,
            user=buyer,
            order_id=order.id,
            order_number=order.order_number,
            group_chat_id=group_id,
            topic_id=order.thread_id,
            subject=f"Order dispute — {order.order_number}",
            body=body,
        )
        return Dispute(ticket, reached_staff)
    except TelegramAPIError as exc:
        logger.warning("Couldn't open the dispute thread for %s (%s)", order.order_number, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — a refund is already parked; never fail it over a group
        logger.error("Unexpected failure opening the dispute thread for %s: %s", order.order_number, exc)
        return None
