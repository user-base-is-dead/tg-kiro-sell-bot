from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.models.order import FundingSource, Order, OrderStatus, RefundState
from app.database.models.order_event import OrderEvent, OrderEventKind
from app.database.repositories.order_event_repo import OrderEventRepo
from app.database.repositories.order_repo import OrderRepo
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

        # A dead order's thread is closed so the group reads as a queue of live work. Refunds are the
        # exception — a cancelled order whose money is still parked is very much unfinished.
        if order.status in (OrderStatus.CANCELLED, OrderStatus.FAILED) and order.refund_state is not RefundState.PARKED:
            await _close(bot, group_id, order)
        elif order.status is OrderStatus.COMPLETED:
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
