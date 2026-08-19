from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.models.order import FundingSource, Order, RefundState
from app.database.models.order_event import OrderEvent, OrderEventActor, OrderEventKind
from app.database.models.support import SupportTicket
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


@dataclass(frozen=True)
class RefundThread:
    """The conversation a refund will be settled in.

    `created` distinguishes a fresh thread from one the buyer already had open. Both are correct
    outcomes — see `open_or_reuse_thread` for why reusing is not a shortcut — but the admin is told
    which, because "a chat has been opened" and "your notice was added to their existing chat" are
    different things to walk into.
    """

    ticket: SupportTicket | None
    created: bool
    reached_staff: bool


def _crypto_note(order: Order, tx_hash: str | None) -> str:
    lines = [
        "💎 <b>You paid for this on chain (USDT, BNB Smart Chain).</b>",
        "A blockchain transfer can't be reversed by us, so this refund is settled by hand.",
    ]
    if tx_hash:
        lines.append(f"Your payment: <code>{escape_html(tx_hash)}</code>")
    lines.append("")
    lines.append("👉 <b>Reply here with the BEP-20 (BSC) address you want the refund sent to.</b>")
    return "\n".join(lines)


def buyer_notice(
    order: Order,
    *,
    reason: str,
    refunded_minor: int,
    refund_event: OrderEvent | None,
    decline_event: OrderEvent,
    ticket: SupportTicket | None,
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

    if ticket is not None:
        lines += [
            f"🎫 Ticket <code>{ticket.ticket_number}</code> is open for this refund — just reply here "
            "and it reaches our team.",
            "",
        ]

    lines.append(f"🔖 Decline ID: <code>{decline_event.event_number}</code>")
    if refund_event is not None:
        lines.append(f"🔖 Refund ID: <code>{refund_event.event_number}</code>")
    lines.append("")
    lines.append("Quote either ID and we can pull up exactly what happened.")
    return "\n".join(lines)


def _thread_subject(order: Order, *, reason: str, refunded_minor: int, refund_event: OrderEvent | None) -> str:
    """The opening message of the refund thread — written for staff, who need to be able to settle it
    without opening the admin panel first."""
    lines = [
        f"Refund — order {order.order_number}",
        "",
        f"Declined reason: {reason}",
        f"Amount parked in Refund Wallet: {format_minor(refunded_minor, order.currency)}",
        f"Paid by: {'crypto (USDT, on chain)' if order.funding_source is FundingSource.CRYPTO else 'wallet balance'}",
    ]
    if refund_event is not None:
        lines.append(f"Refund ID: {refund_event.event_number}")
    if order.funding_source is FundingSource.CRYPTO:
        lines += [
            "",
            "The buyer has been asked for a BEP-20 address. Send the payout, then record it on the "
            "order's Refund Wallet screen so the balance matches what actually went out.",
        ]
    else:
        lines += [
            "",
            "Settle it on the order's Refund Wallet screen — move it into their spendable wallet, or "
            "record a payout if you send it another way.",
        ]
    return "\n".join(lines)


async def open_or_reuse_thread(
    bot: Bot,
    session: AsyncSession,
    *,
    order: Order,
    buyer: User,
    reason: str,
    refunded_minor: int,
    refund_event: OrderEvent | None,
    admin_telegram_id: int | None = None,
) -> RefundThread:
    """Get the refund in front of staff, in a thread the buyer can reply into.

    Reuses an existing open thread rather than opening a second one. That is not a shortcut: a user is
    allowed exactly one live conversation (see `support_service.active_thread`), because their replies
    can only be relayed into one of them — a second thread would be a channel that looks open from
    their side and silently goes nowhere. So if they already have a ticket, or a warranty claim under
    review, the refund notice is posted into that thread.

    Best-effort throughout. A refund that is already parked must not be rolled back because the
    support group is misconfigured; `reached_staff` reports what actually happened so the admin is
    never told a chat opened when none did.
    """
    from app.services import support_service

    subject = _thread_subject(order, reason=reason, refunded_minor=refunded_minor, refund_event=refund_event)
    existing = await SupportRepo(session).get_open_for_user(buyer.id)

    if existing is not None:
        reached = await _post_into_thread(bot, session, ticket=existing, body=subject)
        await _link_thread(session, order, existing, admin_telegram_id=admin_telegram_id, created=False)
        return RefundThread(existing, created=False, reached_staff=reached)

    ticket, reached_staff = await support_service.create_ticket(
        bot,
        session,
        user=buyer,
        category=REFUND_CATEGORY,
        subject=subject,
        support_group_id=get_settings().support_group_id,
    )
    await _link_thread(session, order, ticket, admin_telegram_id=admin_telegram_id, created=True)
    return RefundThread(ticket, created=True, reached_staff=reached_staff)


async def _post_into_thread(bot: Bot, session: AsyncSession, *, ticket: SupportTicket, body: str) -> bool:
    """Drop a staff-side note into an existing ticket's topic. Recorded as a SYSTEM message so the
    thread's own transcript explains where the refund came from."""
    await SupportRepo(session).add_message(
        ticket_id=ticket.id,
        author_type="SYSTEM",
        author_telegram_id=0,
        content=body,
        created_at=datetime.now(UTC),
    )
    group_id = get_settings().support_group_id
    if group_id is None:
        logger.error("SUPPORT_GROUP_ID unset — refund note for %s reached nobody.", ticket.ticket_number)
        return False
    try:
        await bot.send_message(group_id, escape_html(body), message_thread_id=ticket.topic_id)
        return True
    except TelegramAPIError as exc:
        logger.error("Couldn't post refund note into %s (%s)", ticket.ticket_number, exc)
        return False


async def _link_thread(
    session: AsyncSession,
    order: Order,
    ticket: SupportTicket | None,
    *,
    admin_telegram_id: int | None,
    created: bool,
) -> None:
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
        reason="Refund thread opened" if created else "Refund posted into the buyer's open thread",
        reference=ticket.ticket_number,
    )


async def notify_buyer(bot: Bot, buyer: User, text: str) -> bool:
    """Best-effort DM. A blocked bot must not undo a refund that is already parked — the money is in
    their balance and the ticket is in the group either way."""
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
