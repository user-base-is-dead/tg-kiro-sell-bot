from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.models.catalog import FulfillmentMode
from app.database.models.order import Delivery, FundingSource, Order, OrderStatus, RefundState
from app.database.models.order_event import OrderEvent, OrderEventKind
from app.database.models.support import TicketStatus
from app.database.repositories.order_event_repo import OrderEventRepo
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.product_repo import ProductRepo
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


async def _deliveries(session: AsyncSession, order: Order) -> dict[int, Delivery]:
    """The latest delivery per item, keyed by order_item id. A re-fulfilled item has more than one
    row and the newest is the one that says where it stands."""
    ids = [item.id for item in order.items]
    if not ids:
        return {}
    rows = await session.execute(
        select(Delivery).where(Delivery.order_item_id.in_(ids)).order_by(Delivery.id)
    )
    return {row.order_item_id: row for row in rows.scalars()}


async def _order_card(session: AsyncSession, order: Order) -> str:
    """The whole order on one message, so staff never have to leave the group to know what they are
    looking at — and never have to reconstruct it from the event lines underneath.

    Deliberately the same picture as the admin dossier: who bought what, what it cost, how it was
    paid for, whether it has been delivered, why it died, what is owed and whether anybody has been
    paid back. It is edited in place on every action (see `sync`), so it reads as the order's current
    state rather than as the moment it was placed.
    """
    buyer = await UserRepo(session).get_by_id(order.user_id)
    full = await OrderRepo(session).get_by_id(order.id) or order
    deliveries = await _deliveries(session, full)

    who = "—"
    if buyer is not None:
        handle = f"@{escape_html(buyer.username)}" if buyer.username else "no username"
        who = f'<a href="tg://user?id={buyer.telegram_id}">{handle}</a> · <code>{buyer.telegram_id}</code>'

    lines = [
        f"{_STATUS_EMOJI.get(order.status.value, '•')} <b>{order.order_number}</b> — {order.status.value}",
        "",
        f"👤 {who}",
    ]
    if order.placed_at:
        lines.append(f"🕒 Placed: {as_utc(order.placed_at):%d %b %Y, %H:%M} UTC")
    if order.completed_at:
        lines.append(f"📬 Delivered: {as_utc(order.completed_at):%d %b %Y, %H:%M} UTC")
    if order.cancelled_at:
        lines.append(f"🚫 Cancelled: {as_utc(order.cancelled_at):%d %b %Y, %H:%M} UTC")
    if order.cancelled_by_admin_id:
        lines.append(f"👮 By admin: <code>{order.cancelled_by_admin_id}</code>")

    lines += ["", "🛍 <b>Items</b>"]
    for item in full.items:
        unit = format_minor(item.unit_price_minor, order.currency)
        line = f"• {escape_html(item.product_name)}"
        line += f" ×{item.qty} — {unit} each" if item.qty > 1 else f" — {unit}"
        lines.append(line)

        detail = []
        product = (
            await ProductRepo(session).get_by_id(item.product_id) if item.product_id else None
        )
        if product is not None:
            detail.append(
                "🤖 Auto" if product.fulfillment_mode is FulfillmentMode.AUTO else "✋ Manual"
            )
        if item.warranty_days:
            detail.append(f"🛡 {item.warranty_days}d warranty")
        delivery = deliveries.get(item.id)
        if delivery is not None and delivery.delivered_at:
            stamp = f"{as_utc(delivery.delivered_at):%d %b %H:%M}"
            detail.append(f"📬 delivered {stamp}")
            if delivery.delivered_by_admin_id:
                detail.append(f"by <code>{delivery.delivered_by_admin_id}</code>")
        elif order.status is OrderStatus.PROCESSING:
            detail.append("⏳ awaiting fulfilment")
        if detail:
            lines.append(f"   {' · '.join(detail)}")

    paid = "💎 Crypto (USDT, on chain)" if order.funding_source is FundingSource.CRYPTO else "💳 Wallet balance"
    lines += ["", f"💰 Total: <b>{format_minor(order.total_minor, order.currency)}</b>"]
    if order.discount_minor:
        lines.append(
            f"🏷 Subtotal {format_minor(order.subtotal_minor, order.currency)} "
            f"− discount {format_minor(order.discount_minor, order.currency)}"
        )
    lines.append(f"Paid by: {paid}")

    if order.crypto_payment_id:
        tx_hash = await _tx_hash(session, order.crypto_payment_id)
        if tx_hash:
            lines.append(f"🔗 Tx: <code>{escape_html(tx_hash)}</code>")

    if order.failure_reason:
        lines += ["", "🚫 <b>Decline reason</b>", escape_html(order.failure_reason)]

    if order.refund_state is not RefundState.NONE:
        amount = format_minor(order.refund_amount_minor or 0, order.currency)
        label = {
            RefundState.PARKED: f"🟠 <b>{amount}</b> parked in their Refund Wallet — not settled yet",
            RefundState.SETTLED: f"🟢 <b>{amount}</b> refunded and settled",
        }[order.refund_state]
        lines += ["", "💸 <b>Refund</b>", label]
        if order.refund_ticket_id:
            ticket = await SupportRepo(session).get_by_id(order.refund_ticket_id)
            if ticket is not None:
                lines.append(f"🎫 Ticket: <code>{ticket.ticket_number}</code> ({ticket.status.value})")

    if order.status is OrderStatus.PROCESSING:
        lines += ["", "⏳ <b>Needs manual fulfilment</b> — Admin panel → 🛒 Orders"]

    lines += ["", f"🆔 <code>{order.id}</code>"]
    return "\n".join(lines)


async def _tx_hash(session: AsyncSession, crypto_payment_id: int) -> str | None:
    from app.database.models.crypto import CryptoPayment

    payment = await session.get(CryptoPayment, crypto_payment_id)
    return payment.tx_hash if payment else None


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
            card = await bot.send_message(
                group_id, await _order_card(session, order), message_thread_id=order.thread_id
            )
            order.thread_card_message_id = card.message_id
            await session.flush()
        else:
            await _refresh_card(bot, session, group_id, order)

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


async def _refresh_card(bot: Bot, session: AsyncSession, group_id: int, order: Order) -> None:
    """Edit the card at the top of the topic back into the truth.

    Swallows its own failures rather than letting them abort the sync: the event lines below are the
    part that must not be lost, and an unchanged card raises "message is not modified" as a matter of
    course.
    """
    if order.thread_card_message_id is None:
        return
    try:
        await bot.edit_message_text(
            await _order_card(session, order),
            chat_id=group_id,
            message_id=order.thread_card_message_id,
        )
    except TelegramAPIError:
        pass


async def _close(bot: Bot, group_id: int, order: Order) -> None:
    try:
        await bot.close_forum_topic(chat_id=group_id, message_thread_id=order.thread_id)
    except TelegramAPIError:
        # Already closed, or the topic is gone. Neither is worth a warning — this is cosmetic.
        pass


async def reopen(bot: Bot, session: AsyncSession, order: Order) -> None:
    """Bring a closed thread back before posting into it.

    A completed order's topic is closed, and Telegram refuses messages into a closed topic. Something
    can still happen afterwards — money parked on the order gets settled or moved — and that must land
    in the same thread rather than silently going nowhere.
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
