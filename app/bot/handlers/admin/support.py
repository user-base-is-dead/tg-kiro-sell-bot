from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminTicketCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.styles import NEUTRAL, PRIMARY, btn
from app.core.config import get_settings
from app.database.models.support import TicketStatus
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.support_repo import SupportRepo
from app.services.support_service import announce_closure, close_ticket
from app.utils.errors import UserError

router = Router(name="admin.support")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def _list_keyboard(tickets: list) -> InlineKeyboardMarkup:
    rows = [
        [
            btn(
                f"🎫 {tk.ticket_number} — {tk.subject[:30]}",
                AdminTicketCB(action="close", id=str(tk.id)).pack(),
                PRIMARY,
            )
        ]
        for tk in tickets
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[btn("— none —", "noop", NEUTRAL)]])


@router.message(Command("open_tickets"))
async def list_open(message, session: AsyncSession) -> None:
    tickets = await SupportRepo(session).list_open()
    await message.answer(
        f"💬 <b>OPEN TICKETS</b>\n\n{len(tickets)} open. Reply inside each ticket's topic to chat with the user; "
        f"tap a ticket below to close it.",
        reply_markup=_list_keyboard(tickets),
    )


async def _close_order_topic(message: Message) -> None:
    """Retire an order's log thread by hand.

    The reply goes out before the close, because afterwards the topic is read-only and the
    confirmation would have nowhere to land. Best-effort on the close itself: a topic that is already
    closed, or gone, is the state the admin asked for either way.
    """
    await message.reply(
        "🔒 Closing this order's thread. Nothing was talking here — the order's log stays on the "
        "card above, and settling any parked refund reopens it."
    )
    try:
        await message.bot.close_forum_topic(
            chat_id=message.chat.id, message_thread_id=message.message_thread_id
        )
    except TelegramAPIError:
        pass


# Registered on the admin router, which main.py includes BEFORE the relay router. That ordering is
# the whole point: aiogram stops at the first matching handler, so "/close" is consumed here and
# never reaches group_relay — which would otherwise mirror the literal text "/close" to the user
# as though a human had said it.
@router.message(Command("close"))
async def close_from_topic(message: Message, session: AsyncSession, user) -> None:
    settings = get_settings()
    in_support = settings.support_group_id is not None and message.chat.id == settings.support_group_id
    in_orders = settings.orders_group_id is not None and message.chat.id == settings.orders_group_id
    if not (in_support or in_orders):
        await message.reply(
            "Run /close inside a ticket's topic in the support group, or inside a disputed order's "
            "thread in the orders group."
        )
        return
    if message.message_thread_id is None:
        await message.reply("Run this inside the ticket's own topic — the General thread has no ticket attached.")
        return

    ticket = await SupportRepo(session).get_by_topic_in_group(
        message.message_thread_id, message.chat.id, is_support_group=in_support
    )
    if ticket is None:
        # An order's topic is a log, not a conversation — a decline no longer connects the buyer to
        # it, so there is usually no ticket here to close. That used to be answered with "nothing to
        # close", which left the topic of every declined order open forever: its money is parked, so
        # `order_thread_service.sync` deliberately won't close it either. /close is the manual way,
        # and closing the topic is the whole of what it means here.
        if in_orders:
            await _close_order_topic(message)
        else:
            await message.reply("No ticket is attached to this topic.")
        return
    if ticket.status == TicketStatus.CLOSED:
        await message.reply(f"{ticket.ticket_number} is already closed.")
        return

    await close_ticket(session, ticket_id=ticket.id, reason=f"Closed by admin {user.telegram_id}")
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id, action="ticket.close", target_type="ticket", target_id=str(ticket.id)
    )
    # Confirmation goes out before the topic is closed — afterwards the thread is read-only.
    if ticket.order_id:
        await message.reply(
            f"🔒 {ticket.ticket_number} closed and this thread is going read-only. The buyer has been "
            "told, and 💬 Live Chat is open to them again for anything still unresolved."
        )
    else:
        await message.reply(f"🔒 {ticket.ticket_number} closed. The user has been notified.")
    await announce_closure(message.bot, session, ticket)


@router.callback_query(AdminTicketCB.filter(F.action == "close"))
async def do_close(query: CallbackQuery, callback_data: AdminTicketCB, session: AsyncSession, user) -> None:
    try:
        ticket = await close_ticket(session, ticket_id=int(callback_data.id), reason="Closed by admin")
    except UserError:
        await query.answer("Not found.", show_alert=True)
        return

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id, action="ticket.close", target_type="ticket", target_id=str(ticket.id)
    )
    await announce_closure(query.message.bot, session, ticket)

    await query.answer("Ticket closed.")
    tickets = await SupportRepo(session).list_open()
    await query.message.edit_text(f"💬 <b>OPEN TICKETS</b>\n\n{len(tickets)} open.", reply_markup=_list_keyboard(tickets))
