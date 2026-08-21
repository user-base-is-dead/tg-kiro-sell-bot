from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.models.order import FundingSource, Order, RefundState
from app.database.models.order_event import OrderEvent, OrderEventActor, OrderEventKind
from app.database.models.support import SupportTicket, TicketStatus
from app.database.models.user import User
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.support_repo import SupportRepo
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services import order_event_service, wallet_service
from app.utils.errors import UserError
from app.utils.money import format_minor
from app.utils.text import escape_html

logger = logging.getLogger(__name__)

REFUND_CATEGORY = "Refund"


def _crypto_note(order: Order, tx_hash: str | None) -> str:
    lines = [
        "💎 <b>You paid for this on chain (USDT, BNB Smart Chain).</b>",
        "A blockchain transfer can't be reversed by us, so this refund is settled by hand.",
    ]
    if tx_hash:
        lines.append(f"Your payment: <code>{escape_html(tx_hash)}</code>")
    lines.append("")
    # Deliberately NOT "reply here". A decline no longer opens a thread on the buyer's behalf, so a
    # reply to this DM has no open ticket to land in and `dm_relay` drops it without a word — which
    # is the worst possible place to lose a message, since this one carries the address the refund
    # cannot be sent without.
    lines.append(
        "👉 <b>Open 💬 Live Chat and send us the BEP-20 (BSC) address you want the refund sent to.</b>"
    )
    return "\n".join(lines)


def buyer_notice(
    order: Order,
    *,
    reason: str,
    refunded_minor: int,
    refund_event: OrderEvent | None,
    decline_event: OrderEvent,
    tx_hash: str | None = None,
) -> str:
    """What the buyer reads. Written so that nothing they will ask next is missing from it: why, how
    much, where the money is, what happens now, and which ID to quote."""
    lines = [
        "🚫 <b>Order declined and refunded</b>",
        "",
        f"🛒 Order: <code>{order.order_number}</code>",
        f"📝 Reason: {escape_html(reason)}",
        "",
    ]

    if refunded_minor > 0:
        lines += [
            f"💰 <b>{format_minor(refunded_minor, order.currency)}</b> has been moved to your "
            "<b>Refund Balance</b>.",
            "It's held separately from your spendable wallet — our team settles it with you rather "
            "than turning it into shop credit on its own.",
            "",
        ]
    else:
        lines += ["Nothing had been charged for this order, so there is nothing to refund.", ""]

    if order.funding_source is FundingSource.CRYPTO:
        lines += [_crypto_note(order, tx_hash), ""]

    # A decline no longer opens a thread for them. It used to, and the thread it opened was the one
    # conversation they were allowed — so a buyer who wanted to talk about something else was locked
    # out until an admin closed it, and an admin who declined an order for somebody already in a Live
    # Chat had the refund quietly filed into that unrelated chat instead. Pointing at Live Chat costs
    # the buyer one tap and keeps them in charge of whether there is a conversation at all.
    lines += [
        "💬 <b>Want to talk about this?</b> Open 💬 Live Chat from the menu and quote either ID below.",
        "",
    ]

    lines.append(f"🔖 Decline ID: <code>{decline_event.event_number}</code>")
    if refund_event is not None:
        lines.append(f"🔖 Refund ID: <code>{refund_event.event_number}</code>")
    lines.append("")
    lines.append("Quote either ID and we can pull up exactly what happened.")
    return "\n".join(lines)


async def _link_thread(
    session: AsyncSession,
    order: Order,
    ticket: SupportTicket | None,
    *,
    admin_telegram_id: int | None,
) -> None:
    """Record on the order which thread its refund is being settled in.

    Only `adopt_thread` reaches here now, so the thread is always one the buyer was already in — a
    decline opens nothing on their behalf any more.
    """
    if ticket is None:
        return
    order.refund_ticket_id = ticket.id
    await session.flush()
    await order_event_service.record(
        session,
        order,
        OrderEventKind.TICKET_OPENED,
        actor=OrderEventActor.ADMIN if admin_telegram_id else OrderEventActor.SYSTEM,
        actor_telegram_id=admin_telegram_id,
        reason="Refund bound to the buyer's existing thread",
        reference=ticket.ticket_number,
    )


async def adopt_thread(
    session: AsyncSession,
    order: Order,
    ticket_id: int | None,
    *,
    admin_telegram_id: int | None = None,
) -> SupportTicket | None:
    """Hand a thread the buyer is already in over to this order's refund.

    Used when a warranty claim is settled with money: the claim's own thread is where that
    conversation lives, so rather than closing it and opening a second one somewhere else, it is
    bound to the order. That binding is what makes it behave like every other refund thread — the
    24-hour idle sweep leaves it alone, and an admin's /close is what ends it, once the money has
    actually been sent.
    """
    if ticket_id is None:
        return None
    ticket = await SupportRepo(session).get_by_id(ticket_id)
    if ticket is None:
        return None
    ticket.order_id = order.id
    if ticket.status is TicketStatus.CLOSED:
        ticket.status = TicketStatus.OPEN
        ticket.closed_at = None
        ticket.close_reason = None
    await _link_thread(session, order, ticket, admin_telegram_id=admin_telegram_id)
    return ticket


async def notify_buyer(bot: Bot, buyer: User, text: str) -> bool:
    """Best-effort DM. A blocked bot must not undo a refund that is already parked — the money is in
    their balance either way.

    No ForceReply any more. It used to put "Type your message here…" in the input box, which was
    honest while a decline opened a thread for them to type into; now that it doesn't, an input box
    inviting a reply would be pointing at nothing.
    """
    if buyer.chat_id is None:
        return False
    try:
        await bot.send_message(buyer.chat_id, text)
        return True
    except TelegramAPIError as exc:
        logger.warning("Couldn't tell user %s their order was declined (%s)", buyer.telegram_id, exc)
        return False


# ---- Settling: what an admin does with money already parked ----


@dataclass(frozen=True)
class RefundHolder:
    """One buyer with money owed, plus the orders it came from — the settle screen's whole model."""

    user: User
    refund_balance_minor: int
    currency: str
    orders: list[Order]


async def holders(session: AsyncSession, *, limit: int = 50) -> list[RefundHolder]:
    wallets = await WalletRepo(session).list_wallets_with_refunds(limit)
    user_repo = UserRepo(session)
    order_repo = OrderRepo(session)

    result: list[RefundHolder] = []
    for wallet in wallets:
        user = await user_repo.get_by_id(wallet.user_id)
        if user is None:
            continue
        result.append(
            RefundHolder(
                user=user,
                refund_balance_minor=wallet.refund_balance_minor,
                currency=wallet.currency,
                orders=await order_repo.list_refunded_for_user(wallet.user_id, limit=10),
            )
        )
    return result


async def holder_for(session: AsyncSession, user_id: int) -> RefundHolder | None:
    user = await UserRepo(session).get_by_id(user_id)
    if user is None:
        return None
    wallet = await WalletRepo(session).get_or_create(user_id, currency=get_settings().default_currency)
    return RefundHolder(
        user=user,
        refund_balance_minor=wallet.refund_balance_minor,
        currency=wallet.currency,
        orders=await OrderRepo(session).list_refunded_for_user(user_id, limit=10),
    )


async def _settle_orders_if_clear(session: AsyncSession, user_id: int) -> None:
    """Mark a user's parked orders SETTLED once their refund balance reaches zero.

    Balance-driven rather than per-order, because the balance is one pot: an admin paying out $12
    against two declined orders of $6 has settled both, and asking them to tick off each order
    separately is bookkeeping the bot can do itself. While anything is still owed, every order stays
    PARKED — a partial payout leaves a real debt, and calling any part of it settled would hide that.
    """
    wallet = await WalletRepo(session).get_or_create(user_id, currency=get_settings().default_currency)
    if wallet.refund_balance_minor > 0:
        return
    for order in await OrderRepo(session).list_refunded_for_user(user_id, limit=100):
        if order.refund_state is RefundState.PARKED:
            order.refund_state = RefundState.SETTLED
    await session.flush()


async def record_payout(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    note: str,
    admin_telegram_id: int,
    order: Order | None = None,
) -> OrderEvent | None:
    """Write down what the admin actually sent out of band (a USDT transfer, usually).

    The bot sends nothing itself and holds no key — this records a payment that happened elsewhere, so
    the parked balance stops claiming money that has already left. Raises UserError if the balance
    can't cover it.
    """
    if amount_minor <= 0:
        raise UserError("errors.invalid_amount")

    currency = get_settings().default_currency
    await wallet_service.debit_refund_balance(
        session,
        user_id=user_id,
        amount_minor=amount_minor,
        currency=currency,
        # Random rather than derived: two identical payouts of the same amount to the same person are
        # a legitimate thing to record, and a deterministic key would silently swallow the second.
        idempotency_key=f"refpay:{user_id}:{amount_minor}:{secrets.token_hex(6)}",
        note=note[:512],
        ref_type="refund_payout",
        ref_id=order.id if order else None,
    )

    event = None
    target = order or await _newest_parked(session, user_id)
    if target is not None:
        event = await order_event_service.record(
            session,
            target,
            OrderEventKind.REFUND_PAID_OUT,
            actor=OrderEventActor.ADMIN,
            actor_telegram_id=admin_telegram_id,
            amount_minor=amount_minor,
            reason=note,
        )
    await _settle_orders_if_clear(session, user_id)
    return event


async def move_to_spendable(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    admin_telegram_id: int,
    order: Order | None = None,
) -> OrderEvent | None:
    """Turn parked refund money into ordinary spendable balance, because the buyer asked for credit
    instead of a transfer. Raises UserError if the refund balance is short."""
    if amount_minor <= 0:
        raise UserError("errors.invalid_amount")

    await wallet_service.move_refund_to_spendable(
        session,
        user_id=user_id,
        amount_minor=amount_minor,
        currency=get_settings().default_currency,
        note="Moved to spendable wallet by admin",
    )

    event = None
    target = order or await _newest_parked(session, user_id)
    if target is not None:
        event = await order_event_service.record(
            session,
            target,
            OrderEventKind.REFUND_MOVED,
            actor=OrderEventActor.ADMIN,
            actor_telegram_id=admin_telegram_id,
            amount_minor=amount_minor,
            reason="Moved into the buyer's spendable wallet",
        )
    await _settle_orders_if_clear(session, user_id)
    return event


async def _newest_parked(session: AsyncSession, user_id: int) -> Order | None:
    """Which order to hang a settlement event on when the admin settled a balance rather than a
    specific order. The newest unsettled one is the best available answer, and the event's amount and
    note carry the detail regardless."""
    for order in await OrderRepo(session).list_refunded_for_user(user_id, limit=50):
        if order.refund_state is RefundState.PARKED:
            return order
    return None
