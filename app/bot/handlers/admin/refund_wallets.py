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
from app.bot.states.refund_wallet_form import RefundMoveForm, RefundPayoutForm
from app.core.config import get_settings
from app.database.models.order import FundingSource, RefundState
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services import refund_service
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


# ---- The queue: everybody owed money ----


async def render_queue(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    """Who is owed what, largest debt first.

    This screen exists because a parked refund is money the store still holds and somebody is waiting
    for. Without a list of them, the only way to notice one was to remember the order it came from.
    """
    holders = await refund_service.holders(session)
    total = await WalletRepo(session).total_refund_held()
    currency = get_settings().default_currency

    lines = ["💸 <b>REFUND WALLETS</b>", ""]

    if not holders:
        lines += [
            "Nobody is holding refund money right now — nothing to settle.",
            "",
            "When you decline an order, the amount lands in that buyer's Refund Wallet and shows up "
            "here. It is held separately from their spendable balance, so they cannot buy anything "
            "with it until you decide what happens to it.",
        ]
    else:
        lines += [
            f"<b>{format_minor(total, currency)}</b> held across <b>{len(holders)}</b> buyer(s).",
            "",
            "This money is parked, not spendable. Open a buyer to send it on chain and record the "
            "payout, or move it into their normal wallet.",
            "",
        ]
        for holder in holders:
            reasons = [o.order_number for o in holder.orders if o.refund_state is RefundState.PARKED]
            lines.append(
                f"• {_handle(holder.user)} — "
                f"<b>{format_minor(holder.refund_balance_minor, holder.currency)}</b>"
            )
            if reasons:
                lines.append(f"      from {', '.join(f'<code>{r}</code>' for r in reasons[:4])}")

    rows: list[list[InlineKeyboardButton]] = [
        [
            btn(
                f"💸 {_handle(h.user)} — {format_minor(h.refund_balance_minor, h.currency)}",
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
            lines.append(
                f"<code>{as_utc(txn.created_at):%d %b %H:%M}</code> {sign}"
                f"{format_minor(abs(txn.amount_minor), holder.currency)} · {txn.type.value}"
            )
            if txn.admin_note:
                lines.append(f"      {escape_html(txn.admin_note)}")
        lines.append("")

    lines += [
        "<b>What you can do</b>",
        "📤 <b>Record a payout</b> — you sent the money outside the bot (a USDT transfer). This does "
        "not send anything; it writes down what left, so the held balance stops claiming money that "
        "is already gone.",
        "➡️ <b>Move to their wallet</b> — turn it into ordinary spendable balance, if they would "
        "rather have credit than a transfer.",
    ]
    if holder.refund_balance_minor <= 0:
        lines += ["", "Nothing is held for this buyer right now."]

    rows: list[list[InlineKeyboardButton]] = []
    if holder.refund_balance_minor > 0:
        rows.append(
            [
                btn(
                    "📤 Record a payout",
                    AdminRefundCB(action="payout", id=str(user_id), order_id=order_id, src=src).pack(),
                    PRIMARY,
                )
            ]
        )
        rows.append(
            [
                btn(
                    f"➡️ Move all ({format_minor(holder.refund_balance_minor, holder.currency)}) to wallet",
                    AdminRefundCB(action="moveall", id=str(user_id), order_id=order_id, src=src).pack(),
                    SUCCESS,
                )
            ]
        )
        rows.append(
            [
                btn(
                    "➡️ Move part of it",
                    AdminRefundCB(action="move", id=str(user_id), order_id=order_id, src=src).pack(),
                    SUCCESS,
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
    await query.message.edit_text(
        "📤 <b>Record a payout</b>\n\n"
        f"{_handle(holder.user)} · held: "
        f"<b>{format_minor(holder.refund_balance_minor, holder.currency)}</b>\n\n"
        "Send how much you paid out, and a note saying how:\n"
        f"<code>{holder.refund_balance_minor / 100:.2f} sent USDT, tx 0xabc123</code>\n\n"
        "The note is kept on the order's history, so six weeks from now the payout still says where "
        "it went.\n\n"
        "⚠️ This <b>does not send any money</b> — the bot holds no wallet key. Send the transfer "
        "yourself first, then record it here.\n\n"
        "Or /cancel.",
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
    await message.answer("Cancelled — nothing recorded.")


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

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="refund.payout",
        target_type="user",
        target_id=str(data["user_id"]),
        metadata={"amount_minor": amount_minor, "note": note[:256], "event": event.event_number if event else None},
    )
    await state.clear()

    currency = get_settings().default_currency
    lines = [
        f"📤 Recorded a payout of <b>{format_minor(amount_minor, currency)}</b>.",
        f"Note: {escape_html(note)}",
    ]
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


@router.callback_query(AdminRefundCB.filter(F.action == "moveall"))
async def move_all(query: CallbackQuery, callback_data: AdminRefundCB, session: AsyncSession, user) -> None:
    holder = await refund_service.holder_for(session, int(callback_data.id))
    if holder is None or holder.refund_balance_minor <= 0:
        await query.answer("Nothing held for this buyer.", show_alert=True)
        return

    amount_minor = holder.refund_balance_minor
    order = await OrderRepo(session).get_by_id(callback_data.order_id) if callback_data.order_id else None
    try:
        event = await refund_service.move_to_spendable(
            session,
            user_id=holder.user.id,
            amount_minor=amount_minor,
            admin_telegram_id=user.telegram_id,
            order=order,
        )
    except UserError:
        await query.answer("Couldn't move it — the held amount changed.", show_alert=True)
        return

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="refund.move",
        target_type="user",
        target_id=str(holder.user.id),
        metadata={"amount_minor": amount_minor, "event": event.event_number if event else None},
    )
    await query.answer(f"Moved {format_minor(amount_minor, holder.currency)} to their wallet.")

    rendered = await render_settle(
        session, holder.user.id, order_id=callback_data.order_id, src=callback_data.src
    )
    if rendered is not None:
        text, markup = rendered
        await query.message.edit_text(text, reply_markup=markup)
    await _tell_buyer_settled(query, session, holder.user.id, amount_minor, kind="move")


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
    await query.message.edit_text(
        "➡️ <b>Move part of the refund to their wallet</b>\n\n"
        f"{_handle(holder.user)} · held: "
        f"<b>{format_minor(holder.refund_balance_minor, holder.currency)}</b>\n\n"
        "Send how much to move, e.g. <code>5.00</code>.\n\n"
        "It becomes ordinary spendable balance they can buy with. Whatever is left stays held.\n\n"
        "Or /cancel.",
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
    await message.answer("Cancelled — nothing moved.")


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


async def _tell_buyer_settled(event, session: AsyncSession, user_id: int, amount_minor: int, *, kind: str) -> None:
    """Let the buyer know their refund moved. Best-effort: the money has already moved and the ledger
    already says so, so a blocked bot must not undo it."""
    buyer = await UserRepo(session).get_by_id(user_id)
    if buyer is None or buyer.chat_id is None:
        return

    currency = get_settings().default_currency
    if kind == "move":
        text = (
            f"💳 <b>{format_minor(amount_minor, currency)}</b> from your Refund Balance has been moved "
            "into your wallet — you can spend it on anything in the store now."
        )
    else:
        text = (
            f"📤 <b>{format_minor(amount_minor, currency)}</b> of your refund has been sent out. If you "
            "were expecting it on chain, it should land shortly — reply here if it doesn't."
        )

    try:
        await event.bot.send_message(buyer.chat_id, text)
    except Exception as exc:  # noqa: BLE001 — buyer may have blocked the bot
        logger.warning("Couldn't tell user %s about their refund (%s)", buyer.telegram_id, exc)
