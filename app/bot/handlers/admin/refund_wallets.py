from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminMiscCB, AdminOrderCB, AdminRefundCB, AdminUserCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.common import nav_row
from app.bot.keyboards.styles import DANGER, PRIMARY, SUCCESS, btn
from app.bot.states.refund_wallet_form import (
    RefundMoveForm,
    RefundPayoutForm,
    RefundReleaseForm,
    RefundSanctionForm,
)
from app.core.config import get_settings
from app.database.models.order import FundingSource, RefundState
from app.database.models.wallet import TxnAccount
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services import order_thread_service, refund_service
from app.utils.errors import UserError
from app.utils.money import format_minor, parse_to_minor
from app.utils.text import escape_html
from app.utils.time import as_utc

logger = logging.getLogger(__name__)

router = Router(name="admin.refund_wallets")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def _handle(target) -> str:
    return f"@{escape_html(target.username)}" if target.username else f"id {target.telegram_id}"


def _queue_amounts(holder) -> str:
    """The money on a queue button: what is owed, plus what is frozen or sanctioned when there is
    any."""
    parts = []
    if holder.refund_balance_minor or not (holder.frozen_balance_minor or holder.sanctioned_balance_minor):
        parts.append(format_minor(holder.refund_balance_minor, holder.currency))
    if holder.frozen_balance_minor:
        parts.append(f"🧊 {format_minor(holder.frozen_balance_minor, holder.currency)}")
    if holder.sanctioned_balance_minor:
        parts.append(f"🚫 {format_minor(holder.sanctioned_balance_minor, holder.currency)}")
    return " · ".join(parts)


# ---- The queue: everybody owed money ----


async def render_queue(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    """Who is owed what, largest debt first.

    This screen exists because a parked refund is money the store still holds and somebody is waiting
    for. Without a list of them, the only way to notice one was to remember the order it came from.
    """
    holders = await refund_service.holders(session)
    wallet_repo = WalletRepo(session)
    total = await wallet_repo.total_refund_held()
    frozen_total = await wallet_repo.total_frozen_held()
    sanctioned_total = await wallet_repo.total_sanctioned_held()
    currency = get_settings().default_currency

    lines = ["💸 <b>REFUND WALLETS</b>", ""]

    if not holders:
        lines += [
            "Nobody is holding refund or frozen money right now — nothing to settle.",
            "",
            "When you decline an order, you choose where its money goes: the buyer's Refund Wallet "
            "(settled with them later) or their Frozen Wallet (on hold for review). Either way it "
            "shows up here, and neither is spendable until you decide what happens to it.",
        ]
    else:
        summary = f"<b>{format_minor(total, currency)}</b> to settle"
        if frozen_total:
            summary += f" · 🧊 <b>{format_minor(frozen_total, currency)}</b> frozen"
        if sanctioned_total:
            summary += f" · 🚫 <b>{format_minor(sanctioned_total, currency)}</b> sanctioned"
        lines += [
            f"{summary} — across <b>{len(holders)}</b> buyer(s).",
            "",
            "Refund money is parked, not spendable: open a buyer to send it on chain and record the "
            "payout, move it into their normal wallet, or 🚫 Sanction part of it. Frozen money waits "
            "for you to 🧊 Unfreeze it, and sanctioned money for you to ↩️ Release it — both back into "
            "their Refund Wallet.",
            "",
        ]
        for holder in holders:
            parked = [o.order_number for o in holder.orders if o.refund_state is RefundState.PARKED]
            frozen = [o.order_number for o in holder.orders if o.refund_state is RefundState.FROZEN]
            amounts = []
            if holder.refund_balance_minor:
                amounts.append(f"<b>{format_minor(holder.refund_balance_minor, holder.currency)}</b> to settle")
            if holder.frozen_balance_minor:
                amounts.append(f"🧊 <b>{format_minor(holder.frozen_balance_minor, holder.currency)}</b> frozen")
            if holder.sanctioned_balance_minor:
                amounts.append(
                    f"🚫 <b>{format_minor(holder.sanctioned_balance_minor, holder.currency)}</b> sanctioned"
                )
            lines.append(f"• {_handle(holder.user)} — {' · '.join(amounts)}")
            if parked:
                lines.append(f"      from {', '.join(f'<code>{r}</code>' for r in parked[:4])}")
            if frozen:
                lines.append(f"      frozen: {', '.join(f'<code>{r}</code>' for r in frozen[:4])}")

    rows: list[list[InlineKeyboardButton]] = [
        [
            btn(
                f"💸 {_handle(h.user)} — {_queue_amounts(h)}",
                AdminRefundCB(action="view", id=str(h.user.id)).pack(),
                SUCCESS,
            )
        ]
        for h in holders
    ]
    # No "— none —" filler row. An empty queue is the healthy state and the text above already says
    # so; a button that looks tappable and does nothing just invites the tap.
    rows.append(nav_row("en", back_target="admin_panel", home=False))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AdminMiscCB.filter(F.action == "refunds"))
@router.message(Command("refund_wallets"))
async def open_queue(event, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    text, markup = await render_queue(session)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=markup)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup)


# ---- One buyer's settle screen ----


async def render_settle(
    session: AsyncSession, user_id: int, *, order_id: str = "", src: str = "list"
) -> tuple[str, InlineKeyboardMarkup] | None:
    holder = await refund_service.holder_for(session, user_id)
    if holder is None:
        return None

    wallet = await WalletRepo(session).get_or_create(user_id, currency=get_settings().default_currency)
    ledger = await WalletRepo(session).list_refund_transactions(wallet.id, limit=8)

    lines = [
        "💸 <b>Refund Wallet</b>",
        "",
        f"👤 {_handle(holder.user)} · <code>{holder.user.telegram_id}</code>",
        f"🟠 Held for refund: <b>{format_minor(holder.refund_balance_minor, holder.currency)}</b>",
        # Always shown, $0.00 included, so the sanction is visible as a place money can be — the one
        # screen staff settle from should never hide where part of a buyer's refund went.
        f"🚫 Sanctioned: <b>{format_minor(holder.sanctioned_balance_minor, holder.currency)}</b>"
        + (" — blocked until you release it" if holder.sanctioned_balance_minor else ""),
    ]
    if holder.frozen_balance_minor:
        lines.append(
            f"🧊 Frozen: <b>{format_minor(holder.frozen_balance_minor, holder.currency)}</b> — not "
            "refundable until you unfreeze it"
        )
    lines += [
        f"💳 Their spendable balance: {format_minor(wallet.balance_minor, wallet.currency)}",
        "",
    ]

    parked = [o for o in holder.orders if o.refund_state is RefundState.PARKED]
    if parked:
        lines.append("<b>Where it came from</b>")
        for order in parked:
            paid = "💎 crypto" if order.funding_source is FundingSource.CRYPTO else "💳 wallet"
            when = f"{as_utc(order.cancelled_at):%d %b %H:%M}" if order.cancelled_at else "—"
            lines.append(
                f"• <code>{order.order_number}</code> — "
                f"{format_minor(order.refund_amount_minor or 0, order.currency)} · {paid} · {when}"
            )
            if order.failure_reason:
                lines.append(f"      {escape_html(order.failure_reason)}")
        lines.append("")

    frozen = [o for o in holder.orders if o.refund_state is RefundState.FROZEN]
    if frozen:
        lines.append("<b>Frozen</b>")
        for order in frozen:
            paid = "💎 crypto" if order.funding_source is FundingSource.CRYPTO else "💳 wallet"
            when = f"{as_utc(order.cancelled_at):%d %b %H:%M}" if order.cancelled_at else "—"
            lines.append(
                f"• <code>{order.order_number}</code> — "
                f"{format_minor(order.refund_amount_minor or 0, order.currency)} · {paid} · {when}"
            )
            if order.failure_reason:
                lines.append(f"      {escape_html(order.failure_reason)}")
        lines.append("")

    settled = [o for o in holder.orders if o.refund_state is RefundState.SETTLED]
    if settled:
        lines.append("<b>Already settled</b>")
        for order in settled[:5]:
            lines.append(
                f"• <code>{order.order_number}</code> — "
                f"{format_minor(order.refund_amount_minor or 0, order.currency)} ✅"
            )
        lines.append("")

    if ledger:
        lines.append("<b>Refund ledger</b>")
        for txn in ledger:
            sign = "+" if txn.amount_minor > 0 else "−"
            pot = {TxnAccount.FROZEN: " 🧊", TxnAccount.SANCTIONED: " 🚫"}.get(txn.account, "")
            lines.append(
                f"<code>{as_utc(txn.created_at):%d %b %H:%M}</code> {sign}"
                f"{format_minor(abs(txn.amount_minor), holder.currency)} · {txn.type.value}{pot}"
            )
            if txn.admin_note:
                lines.append(f"      {escape_html(txn.admin_note)}")
        lines.append("")

    # Explaining the actions only makes sense while there is something to act on. At zero the
    # buttons are gone anyway, and a "maximum you can enter is $0.00" line is just noise on a screen
    # whose only real message is "this one is done".
    if holder.refund_balance_minor > 0:
        lines += [
            "<b>What you can do</b>",
            "📤 <b>Refund</b> — you are sending the money out yourself (a USDT transfer). Type how much.",
            "➡️ <b>Move to wallet</b> — turn it into spendable balance they can buy with. Type how much.",
            "🚫 <b>Sanction</b> — block it. It stays theirs and shows on their wallet as Sanctioned, but "
            "can't be refunded or spent until you release it. Type how much.",
            "",
            "Each one takes the amount you type, so you can deal with part of it now and the rest "
            "later. The most you can enter is what is held: "
            f"<b>{format_minor(holder.refund_balance_minor, holder.currency)}</b>.",
        ]
    if holder.sanctioned_balance_minor > 0:
        if holder.refund_balance_minor > 0:
            lines.append("")
        lines.append(
            "↩️ <b>Release</b> — lift the sanction on part or all of the "
            f"<b>{format_minor(holder.sanctioned_balance_minor, holder.currency)}</b>: what you type "
            "goes back into their Refund Wallet. They're told."
        )
    if holder.frozen_balance_minor > 0:
        if holder.refund_balance_minor > 0 or holder.sanctioned_balance_minor > 0:
            lines.append("")
        lines.append(
            "🧊 <b>Unfreeze</b> — release all of the frozen "
            f"<b>{format_minor(holder.frozen_balance_minor, holder.currency)}</b> into their Refund "
            "Wallet. They're told, and from there you settle it like any other refund."
        )
    if (
        holder.refund_balance_minor <= 0
        and holder.frozen_balance_minor <= 0
        and holder.sanctioned_balance_minor <= 0
    ):
        lines.append("✅ Nothing is held for this buyer — every refund of theirs is settled.")

    # Two buttons, not three. "Move all" and "Move part of it" were the same action asked two
    # different ways, and the pair made the screen read like there were three unrelated things to
    # choose between. Both prompts now take an amount, and the full held figure is offered as the
    # default inside them — so settling everything is still one number, not one extra button.
    rows: list[list[InlineKeyboardButton]] = []
    if holder.refund_balance_minor > 0:
        rows.append(
            [
                btn(
                    "📤 Refund",
                    AdminRefundCB(action="payout", id=str(user_id), order_id=order_id, src=src).pack(),
                    PRIMARY,
                ),
                btn(
                    "➡️ Move to wallet",
                    AdminRefundCB(action="move", id=str(user_id), order_id=order_id, src=src).pack(),
                    SUCCESS,
                ),
            ]
        )
        rows.append(
            [
                btn(
                    "🚫 Sanction",
                    AdminRefundCB(action="sanction", id=str(user_id), order_id=order_id, src=src).pack(),
                    DANGER,
                )
            ]
        )
    if holder.sanctioned_balance_minor > 0:
        rows.append(
            [
                btn(
                    "↩️ Release sanction",
                    AdminRefundCB(action="release", id=str(user_id), order_id=order_id, src=src).pack(),
                    SUCCESS,
                )
            ]
        )
    if holder.frozen_balance_minor > 0:
        rows.append(
            [
                btn(
                    "🧊 Unfreeze → Refund Wallet",
                    AdminRefundCB(action="unfreeze", id=str(user_id), order_id=order_id, src=src).pack(),
                    PRIMARY,
                )
            ]
        )

    if order_id:
        rows.append([btn("🛒 Open the order", AdminOrderCB(action="view", id=order_id).pack(), PRIMARY)])
    rows.append([_back_button(user_id, src)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _back_button(user_id: int, src: str) -> InlineKeyboardButton:
    """Back goes where the admin actually came from.

    Opened from a user's profile, "Back to refund list" sent them to the queue of everyone owed
    money — a list that, for a buyer holding nothing, does not contain the screen they were just on
    and often has no rows at all. So the label lied and the destination was a dead end.
    """
    if src == "profile":
        return btn("🔙 Back to profile", AdminUserCB(action="view", id=str(user_id)).pack(), DANGER)
    return btn("🔙 Back to refund list", AdminRefundCB(action="list").pack(), DANGER)


@router.callback_query(AdminRefundCB.filter(F.action == "list"))
async def back_to_queue(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    text, markup = await render_queue(session)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(AdminRefundCB.filter(F.action == "view"))
async def view_holder(
    query: CallbackQuery, callback_data: AdminRefundCB, session: AsyncSession, state: FSMContext
) -> None:
    # This is the Back button on both the payout and the move prompt, so it has to drop the form —
    # otherwise the admin's next message is still read as an amount.
    await state.clear()
    rendered = await render_settle(
        session, int(callback_data.id), order_id=callback_data.order_id, src=callback_data.src
    )
    if rendered is None:
        await query.answer("That account no longer exists.", show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


# ---- Releasing frozen money ----


@router.callback_query(AdminRefundCB.filter(F.action == "unfreeze"))
async def unfreeze_holder(
    query: CallbackQuery, callback_data: AdminRefundCB, session: AsyncSession, state: FSMContext, user
) -> None:
    """Move everything in this buyer's Frozen Wallet into their Refund Wallet, and tell them.

    One tap, no amount to type: the frozen pot is released whole (see `refund_service.unfreeze`).
    From then on it is ordinary refund money, settled with the two buttons that appear for it.
    """
    await state.clear()
    user_id = int(callback_data.id)
    try:
        amount, released = await refund_service.unfreeze(
            session, user_id=user_id, admin_telegram_id=user.telegram_id
        )
    except UserError:
        await query.answer("Nothing is frozen for this buyer any more.", show_alert=True)
        return

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="refund.unfreeze",
        target_type="user",
        target_id=str(user_id),
        metadata={"amount_minor": amount, "orders": [o.id for o in released]},
    )
    # Each order's thread gets its "unfrozen" line and a card that no longer says frozen.
    for order in released:
        await order_thread_service.sync(query.bot, session, order)

    currency = get_settings().default_currency
    buyer = await UserRepo(session).get_by_id(user_id)
    if buyer is not None and buyer.telegram_id != user.telegram_id:
        await refund_service.notify_buyer(
            query.bot, buyer, refund_service.unfreeze_notice(amount, currency)
        )

    head = (
        f"🔓 Released <b>{format_minor(amount, currency)}</b> from their Frozen Wallet into their "
        "Refund Wallet.\n\n"
    )
    rendered = await render_settle(
        session, user_id, order_id=callback_data.order_id, src=callback_data.src
    )
    if rendered is None:
        await query.message.edit_text(head.strip())
    else:
        text, markup = rendered
        await query.message.edit_text(head + text, reply_markup=markup)
    await query.answer("Unfrozen.")


# ---- Recording a payout ----


@router.callback_query(AdminRefundCB.filter(F.action == "payout"))
async def prompt_payout(
    query: CallbackQuery, callback_data: AdminRefundCB, state: FSMContext, session: AsyncSession
) -> None:
    holder = await refund_service.holder_for(session, int(callback_data.id))
    if holder is None:
        await query.answer("That account no longer exists.", show_alert=True)
        return

    await state.set_state(RefundPayoutForm.amount)
    await state.update_data(
        user_id=holder.user.id, order_id=callback_data.order_id, src=callback_data.src
    )
    held = format_minor(holder.refund_balance_minor, holder.currency)
    await query.message.edit_text(
        "📤 <b>Refund</b>\n\n"
        f"{_handle(holder.user)} · held: <b>{held}</b>\n\n"
        "<b>Type how much you are refunding.</b>\n"
        f"<code>{holder.refund_balance_minor / 100:.2f}</code> — all of it\n"
        "<code>2.00</code> — part of it, the rest stays held\n\n"
        f"⚠️ Maximum is <b>{held}</b>. Anything higher is refused and nothing changes.\n\n"
        "You can add a note after the amount, and it is kept on the order's history:\n"
        f"<code>{holder.refund_balance_minor / 100:.2f} sent USDT, tx 0xabc123</code>\n\n"
        "ℹ️ The bot holds no wallet key, so it does not send the transfer — you send it, this records "
        "it and takes the amount off the held balance.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    btn(
                        "🔙 Back",
                        AdminRefundCB(
                            action="view",
                            id=str(holder.user.id),
                            order_id=callback_data.order_id,
                            src=callback_data.src,
                        ).pack(),
                        DANGER,
                    )
                ]
            ]
        ),
    )
    await query.answer()


@router.message(Command("cancel"), RefundPayoutForm.amount)
async def cancel_payout(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Nothing recorded — the held balance is unchanged. Reopen it from 💸 Refund Wallets."
    )


@router.message(RefundPayoutForm.amount)
async def apply_payout(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    parts = (message.text or "").strip().split(maxsplit=1)
    raw = parts[0] if parts else ""
    note = parts[1].strip() if len(parts) > 1 else "Paid out by admin"

    try:
        amount_minor = abs(parse_to_minor(raw))
    except ValueError:
        await message.answer(
            "That isn't a valid amount. Send the number first, then the note — "
            "<code>12.00 sent USDT, tx 0xabc</code>:"
        )
        return
    if amount_minor == 0:
        await message.answer("Amount can't be zero.")
        return

    order = await OrderRepo(session).get_by_id(data["order_id"]) if data.get("order_id") else None
    try:
        event = await refund_service.record_payout(
            session,
            user_id=int(data["user_id"]),
            amount_minor=amount_minor,
            note=note,
            admin_telegram_id=user.telegram_id,
            order=order,
        )
    except UserError:
        await message.answer(
            "❌ That's more than is held for this buyer. Nothing changed — check the held amount and "
            "try again."
        )
        return

    # The settlement belongs in the order's own thread, where the decline that caused it is.
    if order is not None:
        await order_thread_service.reopen(message.bot, session, order)

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="refund.payout",
        target_type="user",
        target_id=str(data["user_id"]),
        metadata={"amount_minor": amount_minor, "note": note[:256], "event": event.event_number if event else None},
    )
    await state.clear()

    currency = get_settings().default_currency
    lines = [f"📤 Refunded <b>{format_minor(amount_minor, currency)}</b>."]
    if note and note != "Paid out by admin":
        lines.append(f"Note: {escape_html(note)}")
    if event is not None:
        lines.append(f"🔖 Payout ID: <code>{event.event_number}</code>")
    lines.append("")

    rendered = await render_settle(
        session, int(data["user_id"]), order_id=data.get("order_id", ""), src=data.get("src", "list")
    )
    if rendered is None:
        await message.answer("\n".join(lines))
        return
    text, markup = rendered
    await message.answer("\n".join(lines) + text, reply_markup=markup)
    await _tell_buyer_settled(message, session, int(data["user_id"]), amount_minor, kind="payout")


# ---- Moving parked money into the spendable wallet ----


@router.callback_query(AdminRefundCB.filter(F.action == "move"))
async def prompt_move(
    query: CallbackQuery, callback_data: AdminRefundCB, state: FSMContext, session: AsyncSession
) -> None:
    holder = await refund_service.holder_for(session, int(callback_data.id))
    if holder is None or holder.refund_balance_minor <= 0:
        await query.answer("Nothing held for this buyer.", show_alert=True)
        return

    await state.set_state(RefundMoveForm.amount)
    await state.update_data(
        user_id=holder.user.id, order_id=callback_data.order_id, src=callback_data.src
    )
    held = format_minor(holder.refund_balance_minor, holder.currency)
    await query.message.edit_text(
        "➡️ <b>Move to wallet</b>\n\n"
        f"{_handle(holder.user)} · held: <b>{held}</b>\n\n"
        "<b>Type how much to move.</b>\n"
        f"<code>{holder.refund_balance_minor / 100:.2f}</code> — all of it\n"
        "<code>2.00</code> — part of it, the rest stays held\n\n"
        f"⚠️ Maximum is <b>{held}</b>. Anything higher is refused and nothing changes.\n\n"
        "It becomes ordinary spendable balance they can buy with, immediately.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    btn(
                        "🔙 Back",
                        AdminRefundCB(
                            action="view",
                            id=str(holder.user.id),
                            order_id=callback_data.order_id,
                            src=callback_data.src,
                        ).pack(),
                        DANGER,
                    )
                ]
            ]
        ),
    )
    await query.answer()


@router.message(Command("cancel"), RefundMoveForm.amount)
async def cancel_move(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Nothing moved — the held balance is unchanged. Reopen it from 💸 Refund Wallets."
    )


@router.message(RefundMoveForm.amount)
async def apply_move(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    try:
        amount_minor = abs(parse_to_minor((message.text or "").strip()))
    except ValueError:
        await message.answer("That isn't a valid amount. Try <code>5.00</code>:")
        return
    if amount_minor == 0:
        await message.answer("Amount can't be zero.")
        return

    order = await OrderRepo(session).get_by_id(data["order_id"]) if data.get("order_id") else None
    try:
        event = await refund_service.move_to_spendable(
            session,
            user_id=int(data["user_id"]),
            amount_minor=amount_minor,
            admin_telegram_id=user.telegram_id,
            order=order,
        )
    except UserError:
        await message.answer("❌ That's more than is held for this buyer. Nothing moved.")
        return

    if order is not None:
        await order_thread_service.reopen(message.bot, session, order)

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="refund.move",
        target_type="user",
        target_id=str(data["user_id"]),
        metadata={"amount_minor": amount_minor, "event": event.event_number if event else None},
    )
    await state.clear()

    currency = get_settings().default_currency
    head = f"➡️ Moved <b>{format_minor(amount_minor, currency)}</b> into their spendable wallet.\n"
    if event is not None:
        head += f"🔖 Move ID: <code>{event.event_number}</code>\n"

    rendered = await render_settle(
        session, int(data["user_id"]), order_id=data.get("order_id", ""), src=data.get("src", "list")
    )
    if rendered is None:
        await message.answer(head)
        return
    text, markup = rendered
    await message.answer(head + "\n" + text, reply_markup=markup)
    await _tell_buyer_settled(message, session, int(data["user_id"]), amount_minor, kind="move")


# ---- Sanctioning part of the Refund Wallet, and lifting it ----


def _back_to_settle(holder, callback_data: AdminRefundCB) -> InlineKeyboardMarkup:
    """Back on an amount prompt: the buyer's settle screen, which also drops the form."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn(
                    "🔙 Back",
                    AdminRefundCB(
                        action="view",
                        id=str(holder.user.id),
                        order_id=callback_data.order_id,
                        src=callback_data.src,
                    ).pack(),
                    DANGER,
                )
            ]
        ]
    )


@router.callback_query(AdminRefundCB.filter(F.action == "sanction"))
async def prompt_sanction(
    query: CallbackQuery, callback_data: AdminRefundCB, state: FSMContext, session: AsyncSession
) -> None:
    holder = await refund_service.holder_for(session, int(callback_data.id))
    if holder is None or holder.refund_balance_minor <= 0:
        await query.answer("Nothing is held for refund for this buyer.", show_alert=True)
        return

    await state.set_state(RefundSanctionForm.amount)
    await state.update_data(
        user_id=holder.user.id, order_id=callback_data.order_id, src=callback_data.src
    )
    held = format_minor(holder.refund_balance_minor, holder.currency)
    await query.message.edit_text(
        "🚫 <b>Sanction</b>\n\n"
        f"{_handle(holder.user)} · held for refund: <b>{held}</b>\n\n"
        "<b>Type how much to sanction.</b>\n"
        f"<code>{holder.refund_balance_minor / 100:.2f}</code> — all of it\n"
        "<code>2.00</code> — part of it, the rest stays in their Refund Wallet\n\n"
        f"⚠️ Maximum is <b>{held}</b>. Anything higher is refused and nothing changes.\n\n"
        "Add a reason after the amount if you want — the buyer is shown it:\n"
        f"<code>{holder.refund_balance_minor / 100:.2f} chargeback opened on this payment</code>\n\n"
        "The money stays theirs and shows on their wallet as 🚫 Sanctioned, but it can't be refunded "
        "or spent until you release it.",
        reply_markup=_back_to_settle(holder, callback_data),
    )
    await query.answer()


@router.message(Command("cancel"), RefundSanctionForm.amount)
async def cancel_sanction(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Nothing sanctioned — the held balance is unchanged. Reopen it from 💸 Refund Wallets."
    )


@router.message(RefundSanctionForm.amount)
async def apply_sanction(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    parts = (message.text or "").strip().split(maxsplit=1)
    raw = parts[0] if parts else ""
    reason = parts[1].strip() if len(parts) > 1 else None

    try:
        amount_minor = abs(parse_to_minor(raw))
    except ValueError:
        await message.answer(
            "That isn't a valid amount. Send the number first, then the reason if any — "
            "<code>5.00 chargeback opened</code>:"
        )
        return
    if amount_minor == 0:
        await message.answer("Amount can't be zero.")
        return

    order = await OrderRepo(session).get_by_id(data["order_id"]) if data.get("order_id") else None
    try:
        event = await refund_service.sanction(
            session,
            user_id=int(data["user_id"]),
            amount_minor=amount_minor,
            admin_telegram_id=user.telegram_id,
            reason=reason,
            order=order,
        )
    except UserError:
        await message.answer(
            "❌ That's more than is held for refund for this buyer. Nothing changed — check the held "
            "amount and try again."
        )
        return

    if order is not None:
        await order_thread_service.reopen(message.bot, session, order)

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="refund.sanction",
        target_type="user",
        target_id=str(data["user_id"]),
        metadata={
            "amount_minor": amount_minor,
            "reason": (reason or "")[:256],
            "event": event.event_number if event else None,
        },
    )
    await state.clear()

    currency = get_settings().default_currency
    lines = [f"🚫 Sanctioned <b>{format_minor(amount_minor, currency)}</b>."]
    if reason:
        lines.append(f"Reason: {escape_html(reason)}")
    if event is not None:
        lines.append(f"🔖 Sanction ID: <code>{event.event_number}</code>")
    lines.append("")
    await _answer_with_settle(message, session, data, "\n".join(lines))

    buyer = await UserRepo(session).get_by_id(int(data["user_id"]))
    if buyer is not None:
        await refund_service.notify_buyer(
            message.bot, buyer, refund_service.sanction_notice(amount_minor, currency, reason)
        )


@router.callback_query(AdminRefundCB.filter(F.action == "release"))
async def prompt_release(
    query: CallbackQuery, callback_data: AdminRefundCB, state: FSMContext, session: AsyncSession
) -> None:
    holder = await refund_service.holder_for(session, int(callback_data.id))
    if holder is None or holder.sanctioned_balance_minor <= 0:
        await query.answer("Nothing is sanctioned for this buyer any more.", show_alert=True)
        return

    await state.set_state(RefundReleaseForm.amount)
    await state.update_data(
        user_id=holder.user.id, order_id=callback_data.order_id, src=callback_data.src
    )
    sanctioned = format_minor(holder.sanctioned_balance_minor, holder.currency)
    await query.message.edit_text(
        "↩️ <b>Release sanction</b>\n\n"
        f"{_handle(holder.user)} · sanctioned: <b>{sanctioned}</b>\n\n"
        "<b>Type how much to release.</b>\n"
        f"<code>{holder.sanctioned_balance_minor / 100:.2f}</code> — all of it\n"
        "<code>2.00</code> — part of it, the rest stays sanctioned\n\n"
        f"⚠️ Maximum is <b>{sanctioned}</b>. Anything higher is refused and nothing changes.\n\n"
        "What you release goes back into their Refund Wallet, and you settle it from there as usual.",
        reply_markup=_back_to_settle(holder, callback_data),
    )
    await query.answer()


@router.message(Command("cancel"), RefundReleaseForm.amount)
async def cancel_release(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Nothing released — the sanction is unchanged. Reopen it from 💸 Refund Wallets."
    )


@router.message(RefundReleaseForm.amount)
async def apply_release(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    try:
        amount_minor = abs(parse_to_minor((message.text or "").strip()))
    except ValueError:
        await message.answer("That isn't a valid amount. Try <code>5.00</code>:")
        return
    if amount_minor == 0:
        await message.answer("Amount can't be zero.")
        return

    order = await OrderRepo(session).get_by_id(data["order_id"]) if data.get("order_id") else None
    try:
        event = await refund_service.release_sanction(
            session,
            user_id=int(data["user_id"]),
            amount_minor=amount_minor,
            admin_telegram_id=user.telegram_id,
            order=order,
        )
    except UserError:
        await message.answer("❌ That's more than is sanctioned for this buyer. Nothing was released.")
        return

    if order is not None:
        await order_thread_service.reopen(message.bot, session, order)

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="refund.sanction_release",
        target_type="user",
        target_id=str(data["user_id"]),
        metadata={"amount_minor": amount_minor, "event": event.event_number if event else None},
    )
    await state.clear()

    currency = get_settings().default_currency
    head = f"↩️ Released <b>{format_minor(amount_minor, currency)}</b> back into their Refund Wallet.\n"
    if event is not None:
        head += f"🔖 Release ID: <code>{event.event_number}</code>\n"
    await _answer_with_settle(message, session, data, head)

    buyer = await UserRepo(session).get_by_id(int(data["user_id"]))
    if buyer is not None:
        await refund_service.notify_buyer(
            message.bot, buyer, refund_service.sanction_release_notice(amount_minor, currency)
        )


async def _answer_with_settle(message: Message, session: AsyncSession, data: dict, head: str) -> None:
    """The confirmation, with the buyer's refreshed settle screen under it."""
    rendered = await render_settle(
        session, int(data["user_id"]), order_id=data.get("order_id", ""), src=data.get("src", "list")
    )
    if rendered is None:
        await message.answer(head)
        return
    text, markup = rendered
    await message.answer(head + "\n" + text, reply_markup=markup)


async def _tell_buyer_settled(event, session: AsyncSession, user_id: int, amount_minor: int, *, kind: str) -> None:
    """Let the buyer know their refund moved. Best-effort: the money has already moved and the ledger
    already says so, so a blocked bot must not undo it."""
    buyer = await UserRepo(session).get_by_id(user_id)
    if buyer is None or buyer.chat_id is None:
        return

    currency = get_settings().default_currency
    if kind == "move":
        text = (
            f"👛 <b>{format_minor(amount_minor, currency)}</b> from your Refund Wallet has been moved "
            "into your balance — you can spend it on anything in the shop now."
        )
    else:
        # Not "reply here": paying the refund out is what ends the thread it was argued in, so by
        # the time this arrives there is nothing left listening in this chat. They are pointed at
        # 🎧 Support, which opens a fresh thread staff actually see.
        text = (
            f"📤 <b>{format_minor(amount_minor, currency)}</b> of your refund has been sent out. If you "
            "were expecting it on-chain, it should arrive shortly.\n\n"
            "If it doesn't, open <b>🎧 Support</b> from the menu and tell us — that starts a new "
            "thread with our team."
        )

    try:
        await event.bot.send_message(buyer.chat_id, text)
    except Exception as exc:  # noqa: BLE001 — buyer may have blocked the bot
        logger.warning("Couldn't tell user %s about their refund (%s)", buyer.telegram_id, exc)
