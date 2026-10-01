from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminOrderCB, AdminRefundCB, NavCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.filters.staff import IsStaff
from app.bot.keyboards.common import nav_row
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.bot.states.order_decline_form import OrderDeclineForm, OrderSearchForm
from app.bot.states.order_fulfill_form import OrderFulfillForm
from app.database.models.order import FundingSource, OrderStatus, RefundState
from app.database.models.order_event import OrderEventKind
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.support_repo import SupportRepo
from app.database.repositories.user_repo import UserRepo
from app.locales.i18n import t
from app.services import order_event_service, order_service, order_thread_service, refund_service
from app.utils.errors import UserError
from app.utils.money import format_minor
from app.utils.text import as_admin_wrote_it, escape_html
from app.utils.time import as_utc

logger = logging.getLogger(__name__)

router = Router(name="admin.orders")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

# Fulfil and Decline — the two buttons on an order's "awaiting fulfilment" message in its topic —
# with their prompts, Back buttons and the messages that answer them. Anyone in the orders group can
# work an order from there, admin or not (see app/bot/filters/staff.py); admins can still do it from
# the dossier in their private chat. Everything else in this module stays admin-only.
staff_router = Router(name="admin.orders.staff")
staff_router.message.filter(IsStaff())
staff_router.callback_query.filter(IsStaff())

PAGE_SIZE = 10

STATUS_EMOJI = {
    "PENDING": "🟡",
    "PROCESSING": "🔵",
    "COMPLETED": "🟢",
    "CANCELLED": "🔴",
    "FAILED": "⚠️",
}

# What each history line says it is. The prefix on the event's own ID already encodes the kind, so
# these are the human half of the same fact.
_EVENT_LABEL = {
    OrderEventKind.PLACED: "🛒 Placed",
    OrderEventKind.DELIVERED: "📬 Delivered",
    OrderEventKind.DECLINED: "🚫 Declined",
    OrderEventKind.REFUND_PARKED: "💰 Refund parked",
    OrderEventKind.REFUND_FROZEN: "🧊 Money frozen",
    OrderEventKind.REFUND_UNFROZEN: "🔓 Unfrozen → Refund Wallet",
    OrderEventKind.REFUND_SANCTIONED: "🚫 Refund sanctioned",
    OrderEventKind.SANCTION_RELEASED: "✅ Sanction released → Refund Wallet",
    OrderEventKind.REFUND_PAID_OUT: "📤 Refund paid out",
    OrderEventKind.REFUND_MOVED: "➡️ Moved to wallet",
    OrderEventKind.TICKET_OPENED: "🎫 Ticket opened",
}


def _list_keyboard(orders: list) -> InlineKeyboardMarkup:
    rows = [
        [
            btn(
                f"🔵 {o.order_number} — {format_minor(o.total_minor, o.currency)}",
                AdminOrderCB(action="view", id=o.id).pack(),
                PRIMARY,
            )
        ]
        for o in orders
    ]
    rows = rows or [[btn("— none —", "noop", NEUTRAL)]]
    rows.append(
        [
            btn("🔍 Search by Order ID", AdminOrderCB(action="search").pack(), PRIMARY),
            btn("📜 All orders", AdminOrderCB(action="all").pack(), PRIMARY),
        ]
    )
    rows.append(nav_row("en", back_target="admin_panel", home=False))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detail_keyboard(order) -> InlineKeyboardMarkup:
    """Buttons follow what the order can actually still do, so a cancelled order never offers Fulfill
    and a live one never offers refund controls."""
    rows: list[list[InlineKeyboardButton]] = []

    if order.status is OrderStatus.PROCESSING:
        rows.append([btn("✅ Fulfill", AdminOrderCB(action="fulfill", id=order.id).pack(), SUCCESS)])
    # Declining is refusing to fulfil, so it only exists while the order is still unfulfilled. A
    # delivered order used to offer it too, and pressing it rewrote history: a COMPLETED order the
    # buyer had already been sent turned CANCELLED, with a decline reason on an order that was never
    # declined. Cancelled and failed orders are past it in the other direction.
    if order.status in (OrderStatus.PENDING, OrderStatus.PROCESSING):
        rows.append(
            [btn("🚫 Decline", AdminOrderCB(action="decline", id=order.id).pack(), DANGER)]
        )
    if order.refund_state is RefundState.PARKED:
        rows.append(
            [
                btn(
                    "💸 Settle refund",
                    AdminRefundCB(action="view", id=str(order.user_id), order_id=order.id).pack(),
                    SUCCESS,
                )
            ]
        )
    elif order.refund_state is RefundState.FROZEN:
        rows.append(
            [
                btn(
                    "🧊 Frozen money — open to unfreeze",
                    AdminRefundCB(action="view", id=str(order.user_id), order_id=order.id).pack(),
                    PRIMARY,
                )
            ]
        )

    rows.append([btn("🔙 Back", AdminOrderCB(action="list").pack(), PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _list_text(count: int) -> str:
    return (
        "🛒 <b>PENDING FULFILLMENT</b>\n\n"
        f"{count} order(s) awaiting manual delivery.\n\n"
        "Only <b>Manual</b> products land here — an Auto product delivers its own stock item the "
        "moment payment clears, so it never needs you.\n\n"
        "Tap an order to see what was bought, then:\n"
        "✅ <b>Fulfill</b> — send the buyer their content and mark it delivered\n"
        "🚫 <b>Decline</b> — say why, then choose where the money goes: ↩️ Refund Wallet or 🧊 "
        "Frozen Wallet\n\n"
        "An empty list is the healthy state. Use 🔍 <b>Search</b> or 📜 <b>All orders</b> to reach "
        "anything already delivered or cancelled."
    )


# Both entry points land on the same renderer, which is why it takes `event` rather than a
# CallbackQuery or a Message: the panel's 🛒 Orders button edits the panel in place, /pending_orders
# sends a new message.
@router.callback_query(AdminOrderCB.filter(F.action == "list"))
@router.message(Command("pending_orders"))
async def list_pending(event, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    orders = await OrderRepo(session).list_pending_manual()
    text = _list_text(len(orders))
    markup = _list_keyboard(orders)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=markup)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup)


# ---- The dossier: one order's whole life on one screen ----


async def render_dossier(session: AsyncSession, order_id: str) -> tuple[str, InlineKeyboardMarkup] | None:
    """Everything known about one order, in the order somebody asks it.

    This is what the search bar lands on and what every 🛒 row opens. It exists because the old detail
    screen showed a status and a list of items and nothing else — an admin looking at a cancelled
    order could not tell who cancelled it, why, or whether the buyer had been paid back.
    """
    order = await OrderRepo(session).get_by_id(order_id)
    if order is None:
        return None

    buyer = await UserRepo(session).get_by_id(order.user_id)
    events = await order_event_service.timeline(session, order.id)

    who = "—"
    if buyer is not None:
        handle = f"@{escape_html(buyer.username)}" if buyer.username else "no username"
        who = f"{handle} · <code>{buyer.telegram_id}</code>"

    lines = [
        f"{STATUS_EMOJI.get(order.status.value, '•')} <b>{order.order_number}</b> — {order.status.value}",
        "",
        f"👤 Buyer: {who}",
        f"🆔 Order ID: <code>{order.id}</code>",
    ]

    if order.placed_at:
        lines.append(f"🕒 Placed: {as_utc(order.placed_at):%d %b %Y, %H:%M} UTC")
    if order.completed_at:
        lines.append(f"📬 Delivered: {as_utc(order.completed_at):%d %b %Y, %H:%M} UTC")
    if order.cancelled_at:
        lines.append(f"🚫 Cancelled: {as_utc(order.cancelled_at):%d %b %Y, %H:%M} UTC")
        if order.cancelled_by_admin_id:
            lines.append(f"👮 By admin: <code>{order.cancelled_by_admin_id}</code>")

    lines += ["", "<b>Items</b>"]
    for item in order.items:
        lines.append(
            f"• {escape_html(item.product_name)} ×{item.qty} — "
            f"{format_minor(item.unit_price_minor, order.currency)}"
        )

    paid_by = "💎 Crypto (USDT, on chain)" if order.funding_source is FundingSource.CRYPTO else "💳 Wallet balance"
    lines += ["", f"💰 Total: <b>{format_minor(order.total_minor, order.currency)}</b>", f"Paid by: {paid_by}"]

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
            RefundState.FROZEN: f"🧊 <b>{amount}</b> frozen in their Frozen Wallet — release it from 💸 Refund Wallets",
            RefundState.SETTLED: f"🟢 <b>{amount}</b> refunded and settled",
        }[order.refund_state]
        lines += ["", "💸 <b>Refund</b>", label]
        if order.refund_ticket_id:
            ticket = await SupportRepo(session).get_by_id(order.refund_ticket_id)
            if ticket is not None:
                lines.append(f"🎫 Ticket: <code>{ticket.ticket_number}</code> ({ticket.status.value})")

    lines += ["", "📜 <b>History</b> — every action has its own ID"]
    if not events:
        lines.append("<i>Nothing recorded. This order predates order history.</i>")
    for event in events:
        label = _EVENT_LABEL.get(event.kind, event.kind.value)
        stamp = f"{as_utc(event.created_at):%d %b %H:%M}"
        line = f"<code>{event.event_number}</code> · {stamp} · {label}"
        if event.amount_minor:
            line += f" · {format_minor(event.amount_minor, event.currency or order.currency)}"
        lines.append(line)
        if event.actor_telegram_id:
            lines.append(f"      by <code>{event.actor_telegram_id}</code>")
        if event.reason:
            lines.append(f"      {escape_html(event.reason)}")

    lines += ["", "🔍 Any ID above is searchable from the Orders screen."]
    return "\n".join(lines), _detail_keyboard(order)


async def _tx_hash(session: AsyncSession, crypto_payment_id: int) -> str | None:
    from app.database.models.crypto import CryptoPayment

    payment = await session.get(CryptoPayment, crypto_payment_id)
    return payment.tx_hash if payment else None


@router.callback_query(AdminOrderCB.filter(F.action == "view"))
async def view_order(
    query: CallbackQuery, callback_data: AdminOrderCB, session: AsyncSession, state: FSMContext
) -> None:
    # Clears the form state because this is the Back button on the decline prompt and on the fulfil
    # prompt. Without it, backing out of "why are you declining this?" left the state live and the
    # admin's next message — to anyone, about anything — was swallowed as a decline reason.
    await state.clear()
    rendered = await render_dossier(session, callback_data.id)
    if rendered is None:
        await query.answer("Order not found.", show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


# ---- All orders, paged ----


async def _render_all(session: AsyncSession, page_num: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.utils.pagination import Page

    repo = OrderRepo(session)
    total = await repo.count_all()
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=total)
    orders = await repo.list_all(offset=page.offset, limit=PAGE_SIZE) if total else []

    lines = [
        "📜 <b>ALL ORDERS</b>",
        "",
        f"<b>{total}</b> order(s), newest first.",
    ]
    if not total:
        lines.append("")
        lines.append("Nothing has been ordered yet.")

    rows: list[list[InlineKeyboardButton]] = []
    for order in orders:
        emoji = STATUS_EMOJI.get(order.status.value, "•")
        flag = " 💸" if order.refund_state is RefundState.PARKED else ""
        rows.append(
            [
                btn(
                    f"{emoji} {order.order_number} — {format_minor(order.total_minor, order.currency)}{flag}",
                    AdminOrderCB(action="view", id=order.id).pack(),
                    DANGER if order.status in (OrderStatus.CANCELLED, OrderStatus.FAILED) else PRIMARY,
                )
            ]
        )

    if orders:
        lines += ["", "🟢 Completed  🔵 Processing  🟡 Pending  🔴 Cancelled  💸 refund unsettled"]

    nav: list[InlineKeyboardButton] = []
    if page.has_prev:
        nav.append(btn("◀️ Previous", AdminOrderCB(action="all", page=page.clamped_page - 1).pack(), PRIMARY))
    if page.total_pages > 1:
        nav.append(btn(f"{page.clamped_page}/{page.total_pages}", "noop", NEUTRAL))
    if page.has_next:
        nav.append(btn("Next ▶️", AdminOrderCB(action="all", page=page.clamped_page + 1).pack(), PRIMARY))
    if nav:
        rows.append(nav)

    rows.append([btn("🔍 Search by Order ID", AdminOrderCB(action="search").pack(), PRIMARY)])
    rows.append([btn("🔙 Back", AdminOrderCB(action="list").pack(), DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AdminOrderCB.filter(F.action == "all"))
async def list_all_orders(
    query: CallbackQuery, callback_data: AdminOrderCB, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    text, markup = await _render_all(session, callback_data.page)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


# ---- Search ----


def _search_exits() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("🔙 Back to orders", AdminOrderCB(action="list").pack(), DANGER)],
            nav_row("en", back_target="admin_panel", home=False),
        ]
    )


@router.callback_query(AdminOrderCB.filter(F.action == "search"))
async def prompt_search(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OrderSearchForm.term)
    await query.message.edit_text(
        "🔍 <b>Find an order</b>\n\n"
        "Send any of these:\n"
        "• an order number — <code>ORD-4A9C</code>, or just <code>4A9C</code>\n"
        "• an action ID — <code>DEC-1A0F73</code>, <code>RFD-77C2E9</code>, <code>DLV-…</code>\n"
        "• a ticket number — <code>TCK-2B71</code>\n"
        "• the full order UUID\n\n"
        "You'll get the whole history of that order: what happened, who did it, whether it was "
        "delivered or declined, the reason, and where the refund went.",
        reply_markup=_search_exits(),
    )
    await query.answer()


@router.message(Command("cancel"), OrderSearchForm.term)
async def cancel_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


@router.message(OrderSearchForm.term)
async def do_search(message: Message, state: FSMContext, session: AsyncSession) -> None:
    term = (message.text or "").strip()
    await state.clear()

    results = await OrderRepo(session).search(term)
    if not results:
        await message.answer(
            f"No order matches <b>{escape_html(term)}</b>.\n\n"
            "Order numbers look like <code>ORD-4A9C</code>; action IDs carry a prefix "
            "(<code>DEC-</code>, <code>RFD-</code>, <code>DLV-</code>).",
            reply_markup=_search_exits(),
        )
        return

    if len(results) == 1:
        rendered = await render_dossier(session, results[0].id)
        if rendered is not None:
            text, markup = rendered
            await message.answer(text, reply_markup=markup)
            return

    rows = [
        [
            btn(
                f"{STATUS_EMOJI.get(o.status.value, '•')} {o.order_number} — "
                f"{format_minor(o.total_minor, o.currency)}",
                AdminOrderCB(action="view", id=o.id).pack(),
                PRIMARY,
            )
        ]
        for o in results
    ]
    rows.append([btn("🔙 Back to orders", AdminOrderCB(action="list").pack(), DANGER)])
    await message.answer(
        f"Found <b>{len(results)}</b> match(es) for <b>{escape_html(term)}</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ---- Decline ----


@staff_router.callback_query(AdminOrderCB.filter(F.action.in_(["decline", "cancel"])))
async def prompt_decline(
    query: CallbackQuery, callback_data: AdminOrderCB, state: FSMContext, session: AsyncSession
) -> None:
    """Ask why before anything moves.

    The reason is not optional bookkeeping — it is what the buyer is shown, and it is the only thing
    that makes the decline explicable a month later. The old button refunded immediately and left a
    generic "Cancelled by admin" behind, which answered nobody's question.
    """
    order = await OrderRepo(session).get_by_id(callback_data.id)
    if order is None:
        await query.answer("Order not found.", show_alert=True)
        return
    if order.status in (OrderStatus.CANCELLED, OrderStatus.FAILED):
        await query.answer("This order is already cancelled.", show_alert=True)
        return
    if order.status is OrderStatus.COMPLETED:
        # A button from before this rule, still sitting in somebody's chat history.
        await query.answer(
            "This order was already delivered — it can't be declined. Settle it through the "
            "buyer's Refund Wallet or a warranty claim.",
            show_alert=True,
        )
        return

    await state.set_state(OrderDeclineForm.reason)
    await state.update_data(order_id=order.id, thread_id=query.message.message_thread_id)

    paid_by = "💎 crypto (USDT, on chain)" if order.funding_source is FundingSource.CRYPTO else "💳 wallet balance"
    amount = format_minor(order.total_minor, order.currency)
    charged = order.payment_txn_id is not None and order.total_minor > 0

    body = (
        "🚫 <b>Decline order</b>\n\n"
        f"🛒 <code>{order.order_number}</code> · {amount}\n"
        f"Paid by: {paid_by}\n\n"
        "<b>Send the reason for declining this order.</b>\n"
        "The buyer reads it word for word, and it's saved against the order forever — it shows up in "
        "their order history and in your search results.\n\n"
        "When you send it:\n"
        "• the order is cancelled and any reserved stock goes back on the shelf\n"
        + (
            f"• you choose where the {amount} goes: <b>↩️ Refund Wallet</b> (settle it with them "
            "later) or <b>🧊 Frozen Wallet</b> (on hold — they see it but can't spend it or get it "
            "back)\n"
            if charged
            else "• nothing was charged, so there is no money to place\n"
        )
        + "\nPress <b>Back</b> to leave it alone — the order stays exactly as it is, still waiting for "
        "you to fulfil or decline it."
    )
    # Same rule as the fulfil prompt: in the order's thread this is a new message, because the one
    # it was pressed on is the thread's own record of the order arriving.
    in_group = query.message.chat.type in ("group", "supergroup")
    back = (
        AdminOrderCB(action="decline_cancel", id=order.id)
        if in_group
        else AdminOrderCB(action="view", id=order.id)
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[[btn("🔙 Back", back.pack(), DANGER)]])
    if in_group:
        await query.message.answer(body, reply_markup=markup)
    else:
        await query.message.edit_text(body, reply_markup=markup)
    await query.answer()


@staff_router.callback_query(AdminOrderCB.filter(F.action == "decline_cancel"))
async def cancel_decline_in_thread(query: CallbackQuery, state: FSMContext) -> None:
    """Back on the in-thread decline prompt: drop the state and say so where it was pressed."""
    await state.clear()
    await query.message.edit_text(
        "Left alone — nothing was declined. The order is still waiting for you to fulfil or decline "
        "it, on the card above."
    )
    await query.answer()


# Still registered, so anyone who types /cancel out of habit is not stuck — it just isn't advertised
# on the prompt any more. Back is the documented way out, because it returns to the order with both
# Fulfil and Decline on it: an order left neither fulfilled nor declined is still somebody's unmet
# purchase, and "Cancelled — the order is untouched" read like the job was finished.
@staff_router.message(Command("cancel"), OrderDeclineForm.reason)
async def cancel_decline(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Left alone — nothing was declined. The order is still waiting for you to fulfil or decline "
        "it; find it again under 🛒 Orders."
    )


@staff_router.message(OrderDeclineForm.reason)
async def receive_decline_reason(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    """The reason for a decline. If money was taken, this asks where it goes before anything moves;
    if nothing was charged, the decline goes straight through and the receipt comes back here.
    """
    data = await state.get_data()
    # Started in an order's topic? Then only that topic can answer it — see the fulfil handler for
    # why a group with many threads needs this and a private chat does not.
    prompt_thread = data.get("thread_id")
    if prompt_thread is not None and message.message_thread_id != prompt_thread:
        raise SkipHandler

    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Send the reason as text — it's what the buyer will read:")
        return

    order_id = data.get("order_id")
    if not order_id:
        await state.clear()
        await message.answer("That order is no longer in progress.")
        return

    order = await OrderRepo(session).get_by_id(order_id)
    if order is None or order.status not in (OrderStatus.PENDING, OrderStatus.PROCESSING):
        await state.clear()
        await message.answer("❌ This order can no longer be declined (already cancelled or delivered).")
        return

    if order.payment_txn_id is not None and order.total_minor > 0:
        # Money was taken, so it has to go somewhere — and where is the admin's call, made now that the
        # reason is written. Nothing has moved yet: Back from here still leaves the order untouched.
        await state.update_data(reason=reason)
        await state.set_state(OrderDeclineForm.destination)
        await message.answer(
            _destination_prompt(order, reason),
            reply_markup=_destination_keyboard(
                order.id, in_group=message.chat.type in ("group", "supergroup")
            ),
        )
        return

    # Nothing was charged: there is no money to place, so the decline goes straight through.
    await state.clear()
    text, markup = await _complete_decline(
        message.bot, session, user, order_id=order_id, reason=reason, freeze=False
    )
    await message.answer(text, reply_markup=markup)


def _destination_prompt(order, reason: str) -> str:
    amount = format_minor(order.total_minor, order.currency)
    return (
        f"💰 <b>Where should {amount} go?</b>\n\n"
        f"🛒 <code>{order.order_number}</code>\n"
        f"📝 Reason: {escape_html(reason)}\n\n"
        "↩️ <b>Refund Wallet</b> — the usual. Held for the buyer until you settle it: send it back to "
        "them, or move it into their balance.\n"
        "🧊 <b>Frozen Wallet</b> — on hold for review. The buyer sees it but can't spend it or get it "
        "refunded until you unfreeze it (💸 Refund Wallets → the buyer → 🧊 Unfreeze).\n\n"
        "Press <b>Back</b> to leave it alone — nothing has been declined yet."
    )


def _destination_keyboard(order_id: str, *, in_group: bool) -> InlineKeyboardMarkup:
    # Same Back as the reason prompt: cancel in place inside an order's thread, return to the
    # dossier in a private chat.
    back = (
        AdminOrderCB(action="decline_cancel", id=order_id)
        if in_group
        else AdminOrderCB(action="view", id=order_id)
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn("↩️ Refund Wallet", AdminOrderCB(action="decline_refund", id=order_id).pack(), SUCCESS),
                btn("🧊 Freeze", AdminOrderCB(action="decline_freeze", id=order_id).pack(), PRIMARY),
            ],
            [btn("🔙 Back", back.pack(), DANGER)],
        ]
    )


@staff_router.message(OrderDeclineForm.destination)
async def destination_needs_a_button(message: Message, state: FSMContext) -> None:
    """Text typed while the wallet question is open. Answered here rather than left to fall through,
    because the admin guard would read an unhandled message in an admin state as an intruder."""
    data = await state.get_data()
    prompt_thread = data.get("thread_id")
    if prompt_thread is not None and message.message_thread_id != prompt_thread:
        raise SkipHandler
    await message.answer("Tap a button above — ↩️ Refund Wallet or 🧊 Freeze — or 🔙 Back to leave it.")


@staff_router.callback_query(AdminOrderCB.filter(F.action.in_(["decline_refund", "decline_freeze"])))
async def choose_decline_destination(
    query: CallbackQuery, callback_data: AdminOrderCB, state: FSMContext, session: AsyncSession, user
) -> None:
    """The second half of a decline: the admin picked where the money goes, so now it all happens."""
    if not query.message:
        return
    data = await state.get_data()
    if await state.get_state() != OrderDeclineForm.destination.state or data.get("order_id") != callback_data.id:
        # A button from a decline that was abandoned, finished elsewhere, or replaced by a newer one.
        await query.answer(
            "This decline isn't waiting for an answer any more — start it again from the order.",
            show_alert=True,
        )
        return

    await state.clear()
    text, markup = await _complete_decline(
        query.bot,
        session,
        user,
        order_id=callback_data.id,
        reason=data.get("reason") or "",
        freeze=callback_data.action == "decline_freeze",
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


async def _complete_decline(
    bot, session: AsyncSession, user, *, order_id: str, reason: str, freeze: bool
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Decline, record it, tell the buyer, update the order's thread — and hand back the receipt.

    Shared by the two ways a decline finishes: straight after the reason when nothing was charged,
    and after the admin picks a wallet for the money.

    The receipt is the last screen and it carries its own way out (🏠 Home). It used to push a full
    Home screen as a second message, which buried the receipt — the one thing worth reading — under a
    wall of menu copy the admin had not asked for.
    """
    try:
        declined = await order_service.decline_order(
            session, order_id=order_id, reason=reason, admin_telegram_id=user.telegram_id, freeze=freeze
        )
    except UserError:
        return "❌ This order can no longer be declined (already cancelled or delivered).", None

    order = declined.order
    frozen = order.refund_state is RefundState.FROZEN

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="order.decline",
        target_type="order",
        target_id=order.id,
        metadata={
            "reason": reason[:256],
            "refunded_minor": declined.refunded_minor,
            "destination": "frozen" if frozen else "refund",
            "decline_event": declined.decline_event.event_number,
            "refund_event": declined.refund_event.event_number if declined.refund_event else None,
            "funding_source": order.funding_source.value,
        },
    )

    buyer = await UserRepo(session).get_by_id(order.user_id)
    if buyer is not None:
        tx_hash = await _tx_hash(session, order.crypto_payment_id) if order.crypto_payment_id else None
        # An admin declining their OWN order is the same person on both ends, and they were getting
        # the buyer's DM and the receipt back to back — two near-identical messages in one chat. The
        # receipt is strictly more informative, so the DM is skipped and the receipt says so.
        self_decline = buyer.telegram_id == user.telegram_id
        notified = not self_decline and await refund_service.notify_buyer(
            bot,
            buyer,
            refund_service.buyer_notice(
                order,
                reason=reason,
                refunded_minor=declined.refunded_minor,
                refund_event=declined.refund_event,
                decline_event=declined.decline_event,
                tx_hash=tx_hash,
                frozen=frozen,
            ),
        )
    else:
        notified = False
        self_decline = False

    # Reopen rather than sync: a delivered order's topic is closed, and a decline after delivery has
    # to land in the same thread instead of silently going nowhere.
    await order_thread_service.reopen(bot, session, order)

    rows = [[btn("📜 Open the order", AdminOrderCB(action="view", id=order.id).pack(), PRIMARY)]]
    if declined.refunded_minor > 0:
        rows.append(
            [
                btn(
                    "🧊 View frozen money" if frozen else "💸 Settle refund now",
                    AdminRefundCB(action="view", id=str(order.user_id), order_id=order.id).pack(),
                    PRIMARY if frozen else SUCCESS,
                )
            ]
        )
    # The way out lives on the receipt itself.
    rows.append(
        [
            btn("🛒 Orders", AdminOrderCB(action="list").pack(), PRIMARY),
            btn("🏠 Home", NavCB(target="home").pack(), DANGER),
        ]
    )
    receipt = _receipt(order, declined, buyer, notified=notified, self_decline=self_decline)
    return receipt, InlineKeyboardMarkup(inline_keyboard=rows)


def _receipt(order, declined, buyer, *, notified: bool, self_decline: bool = False) -> str:
    who = "the buyer"
    if buyer is not None:
        who = f"@{escape_html(buyer.username)}" if buyer.username else f"id {buyer.telegram_id}"
    frozen = order.refund_state is RefundState.FROZEN
    amount = format_minor(declined.refunded_minor, order.currency)

    lines = [
        "🚫 <b>Declined — money frozen</b>" if frozen else "🚫 <b>Declined and Refunded</b>",
        "",
        f"🛒 Order: <code>{order.order_number}</code>",
        f"👤 Buyer: {who}",
        f"📝 Reason: {escape_html(order.failure_reason or '')}",
        "",
    ]

    if declined.refunded_minor > 0 and frozen:
        lines.append(
            f"🧊 <b>{amount}</b> frozen in their <b>Frozen Wallet</b> — they can see it, but they "
            "can't spend it or get it refunded."
        )
        lines.append(
            "🔓 When you're ready: 💸 Refund Wallets → this buyer → 🧊 Unfreeze. The money moves to "
            "their Refund Wallet and is settled from there as usual."
        )
    elif declined.refunded_minor > 0:
        lines.append(
            f"💰 <b>{amount}</b> parked in their <b>Refund Wallet</b> — held separately, they cannot "
            "spend it."
        )
        if order.funding_source is FundingSource.CRYPTO:
            lines.append(
                "💎 They paid on chain, so nothing has been sent back yet. They've been asked to open "
                "🎧 Support with their BEP-20 address — send the USDT, then record the payout so the "
                "balance matches reality."
            )
        else:
            lines.append(
                "➡️ Settle it when you're ready: move it into their spendable wallet, or record a "
                "payout if you send it another way."
            )
    else:
        lines.append("Nothing had been charged, so there was nothing to refund.")

    lines += ["", f"🔖 Decline ID: <code>{declined.decline_event.event_number}</code>"]
    if declined.refund_event is not None:
        label = "Freeze ID" if frozen else "Refund ID"
        lines.append(f"🔖 {label}: <code>{declined.refund_event.event_number}</code>")

    # No ticket is opened on their behalf any more. It used to be, and it was the one conversation
    # they were allowed — so a decline could lock a buyer out of support over something unrelated,
    # and a buyer who already had a chat open got the refund filed into it instead of into the order.
    lines += [
        "",
        "🎧 No chat was opened. They've been told to use 🎧 Support if they want to talk about it, "
        "quoting the IDs above.",
    ]
    if self_decline:
        lines.append("ℹ️ This was your own order, so no separate buyer DM was sent — this is it.")
    elif not notified:
        lines.append("⚠️ Couldn't DM the buyer (they may have blocked the bot).")

    lines += ["", "Search either ID above to pull this order up again."]
    return "\n".join(lines)


# ---- Fulfil ----


@staff_router.callback_query(AdminOrderCB.filter(F.action == "fulfill"))
async def start_fulfill(query: CallbackQuery, callback_data: AdminOrderCB, state: FSMContext) -> None:
    await state.set_state(OrderFulfillForm.payload)
    await state.update_data(order_id=callback_data.id, thread_id=query.message.message_thread_id)
    body = (
        "✅ <b>Fulfil this order</b>\n\n"
        "Send the delivery content now.\n\n"
        "The buyer receives it <b>exactly as you send it</b> — if you want a tappable copy box, "
        "format it as code yourself; otherwise it arrives as plain text.\n\n"
        "Press <b>Back</b> to leave it alone — the order stays in the queue."
    )
    # In the order's thread the prompt is a new message, not an edit. The message it was pressed on
    # is the "awaiting fulfilment" record in the thread's history, and overwriting it would erase
    # what the thread exists to keep. Back cancels in place there, because the dossier this normally
    # returns to is the card already sitting at the top of the same topic.
    in_group = query.message.chat.type in ("group", "supergroup")
    back = (
        AdminOrderCB(action="fulfill_cancel", id=callback_data.id)
        if in_group
        else AdminOrderCB(action="view", id=callback_data.id)
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[[btn("🔙 Back", back.pack(), DANGER)]])
    # Same reasoning as the decline prompt: this screen had no button at all, so the only way out was
    # knowing to type /cancel — and an order left neither fulfilled nor declined is still a buyer
    # waiting. Back returns to the order, where both actions are on screen.
    if in_group:
        await query.message.answer(body, reply_markup=markup)
    else:
        await query.message.edit_text(body, reply_markup=markup)
    await query.answer()


@staff_router.callback_query(AdminOrderCB.filter(F.action == "fulfill_cancel"))
async def cancel_fulfill_in_thread(query: CallbackQuery, state: FSMContext) -> None:
    """Back on the in-thread fulfil prompt: drop the state and say so where it was pressed."""
    await state.clear()
    await query.message.edit_text(
        "Left alone — nothing was delivered. The order is still in the queue under 🛒 Orders."
    )
    await query.answer()


@staff_router.message(Command("cancel"), OrderFulfillForm.payload)
async def cancel_fulfill(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Left alone — nothing was delivered. The order is still in the queue under 🛒 Orders."
    )


@staff_router.message(OrderFulfillForm.payload)
async def receive_fulfill_payload(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    # Started in an order's topic? Then only that topic can answer it. FSM state is keyed by chat
    # and user, and the orders group is one chat with many topics — without this, an admin who
    # opened the prompt in one thread and then went to talk to a buyer in another would have that
    # message delivered as somebody else's product. Skipping hands it to the relay, where it belongs.
    prompt_thread = data.get("thread_id")
    if prompt_thread is not None and message.message_thread_id != prompt_thread:
        raise SkipHandler

    try:
        order = await order_service.fulfill_manual_order(
            session,
            order_id=data["order_id"],
            delivery_payload=as_admin_wrote_it(message),
            admin_telegram_id=user.telegram_id,
        )
    except UserError:
        await message.answer("This order can no longer be fulfilled (already completed/cancelled).")
        await state.clear()
        return

    await state.clear()
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id, action="order.fulfill", target_type="order", target_id=order.id
    )
    await order_thread_service.sync(message.bot, session, order)

    # The buyer is sent their goods BEFORE the receipt is composed, so the receipt can state whether
    # the delivery actually landed.
    #
    # Deliberately NOT skipped when the person fulfilling is also the buyer, unlike the decline path
    # above. That guard is right there because a decline receipt restates everything the buyer's DM
    # would have said, so the DM is pure duplication. Here it is the opposite: the receipt carries no
    # payload, so skipping the DM meant the goods were never delivered at all — the order flipped to
    # COMPLETED while the buyer's chat sat on "🛠️ Preparing your order" for ever. A staff member in
    # the orders group buying from the shop they work for hit exactly that, and so does anyone
    # testing the flow with one account. Fulfilling in a group and receiving in a DM are two
    # different chats anyway; in the one case where they are the same chat, a second copy of the
    # content the admin just typed is a far better failure than silence.
    buyer = await UserRepo(session).get_by_id(order.user_id)
    sent_to_buyer = False
    problem = "they have no account row to message"
    if buyer is not None:
        # `chat_id` is only captured from a private message, so it can still be unset for someone who
        # has only ever pressed buttons in a group. `telegram_id` addresses the same private chat and
        # is what every other notifier here falls back to — `buyer.chat_id` alone used to drop the
        # delivery silently.
        try:
            warranty_days = order.items[0].warranty_days if order.items else 0
            # Word for word the message an auto-delivered buyer gets. A hand-fulfilled order is the
            # same purchase with a slower shelf behind it, and it used to arrive looking like a
            # different, more improvised thing — no warranty line, its own heading, its own layout.
            await message.bot.send_message(
                buyer.chat_id or buyer.telegram_id,
                t(
                    "orders.auto_delivery",
                    buyer.locale,
                    payload=as_admin_wrote_it(message),
                    warranty_days=warranty_days,
                ),
            )
            sent_to_buyer = True
        except Exception as exc:  # noqa: BLE001 — the order is fulfilled; report, never raise
            problem = f"the bot couldn't message them — {exc}"
            logger.warning(
                "Delivery for %s couldn't be sent to buyer %s (%s)",
                order.order_number,
                order.user_id,
                exc,
            )

    receipt = f"✅ Order <code>{order.order_number}</code> marked delivered."
    if sent_to_buyer:
        receipt += "\n📬 The content has been sent to the buyer."
    else:
        # Loud, because this is the one failure that leaves a paid order undelivered while the record
        # says otherwise — and nothing retries it.
        receipt += (
            f"\n\n⚠️ <b>The buyer has NOT received it</b> — {problem}.\n"
            "The order is already marked delivered, so nothing will retry on its own. Send them the "
            "content above yourself, or open 🎧 Support to reach them."
        )

    # Carries its own exits, like the decline receipt — a bare "marked delivered" left the admin on a
    # dead message with nothing to press.
    await message.answer(
        receipt,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("📜 Open the order", AdminOrderCB(action="view", id=order.id).pack(), PRIMARY)],
                [
                    btn("🛒 Orders", AdminOrderCB(action="list").pack(), PRIMARY),
                    btn("🏠 Home", NavCB(target="home").pack(), DANGER),
                ],
            ]
        ),
    )
