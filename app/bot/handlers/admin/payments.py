from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminPaymentCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.common import nav_row
from app.bot.keyboards.styles import DANGER, PRIMARY, SUCCESS, btn
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services import wallet_service
from app.utils.errors import UserError
from app.utils.money import format_minor
from app.utils.time import as_utc

router = Router(name="admin.payments")
router.callback_query.filter(IsAdmin())


async def _describe(session: AsyncSession, txn) -> str:
    """One line per request: who is asking, for how much, and when."""
    from app.database.models.wallet import Wallet

    wallet = await session.get(Wallet, txn.wallet_id)
    requester = await UserRepo(session).get_by_id(wallet.user_id) if wallet else None
    who = f"@{requester.username}" if requester and requester.username else (
        f"<code>{requester.telegram_id}</code>" if requester else "unknown user"
    )
    amount = format_minor(txn.amount_minor, wallet.currency if wallet else "USD")
    when = f"{as_utc(txn.created_at):%d %b %H:%M}" if txn.created_at else "—"
    proof = f"\n   ↳ <i>{txn.proof[:120]}</i>" if txn.proof else ""
    return f"<b>#{txn.id}</b> · {amount} · {who} · {when}{proof}"


async def _render(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    """Single source of truth for this screen — approve and reject both redraw it, and three
    copies of the same f-string is how they drift apart."""
    txns = await WalletRepo(session).list_pending_topups()

    header = (
        "💰 <b>PENDING TOP-UPS</b>\n\n"
        "Manual top-up requests waiting on your decision.\n\n"
        "<b>How this works:</b> a user submits a payment they made outside the bot along with "
        "proof of it. Nothing is credited yet — their balance only moves when you approve here.\n\n"
        "✅ <b>Approve</b> credits the amount to their wallet and messages them straight away.\n"
        "❌ <b>Reject</b> closes the request without crediting anything.\n\n"
        "Both are final: a request can only be reviewed once, and every decision is written to the "
        "audit log with your name on it."
    )

    if not txns:
        body = (
            "━━━━━━━━━━━━━━━━━━\n"
            "📭 <b>Nothing to review.</b>\n\n"
            "Top-ups now go through the automatic crypto flow (USDT on BNB Chain), which credits "
            "wallets on its own without asking you. Requests only appear here if the manual "
            "payment provider is put back in front of users — so an empty list is the normal, "
            "healthy state."
        )
    else:
        # An explicit loop, not a generator expression: `await` inside one makes it an *async*
        # generator, which str.join cannot consume — that raised TypeError on the first real
        # request, the exact path an always-empty queue hides.
        described = []
        for i, txn in enumerate(txns, start=1):
            described.append(f"{i}. {await _describe(session, txn)}")
        body = f"━━━━━━━━━━━━━━━━━━\n<b>{len(txns)} awaiting review:</b>\n\n" + "\n".join(described)

    # Approve/reject sit side by side on every row — green vs red is what stops a mis-tap here.
    rows = [
        [
            btn(
                f"✅ #{t.id} {format_minor(t.amount_minor, 'USD')}",
                AdminPaymentCB(action="approve", id=str(t.id)).pack(),
                SUCCESS,
            ),
            btn("❌ Reject", AdminPaymentCB(action="reject", id=str(t.id)).pack(), DANGER),
        ]
        for t in txns
    ]
    rows.append([btn("🔄 Refresh", AdminPaymentCB(action="list").pack(), PRIMARY)])
    rows.append(nav_row("en", back_target="admin_panel"))
    return f"{header}\n\n{body}", InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AdminPaymentCB.filter(F.action == "list"))
async def list_pending(query: CallbackQuery, session: AsyncSession) -> None:
    text, markup = await _render(session)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(AdminPaymentCB.filter(F.action == "approve"))
async def approve(query: CallbackQuery, callback_data: AdminPaymentCB, session: AsyncSession, user) -> None:
    from app.database.models.wallet import WalletTransaction

    txn = await session.get(WalletTransaction, int(callback_data.id))
    if txn is None:
        await query.answer("Not found.", show_alert=True)
        return

    try:
        approved = await wallet_service.approve_topup(session, transaction_id=txn.id, admin_telegram_id=user.telegram_id)
    except UserError:
        await query.answer("Already reviewed.", show_alert=True)
        return

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="topup.approve",
        target_type="wallet_transaction",
        target_id=str(approved.id),
        metadata={"amount_minor": approved.amount_minor},
    )
    await query.answer("Approved.")
    text, markup = await _render(session)
    await query.message.edit_text(text, reply_markup=markup)

    # Notify the buyer directly (best-effort — they may have blocked the bot).
    from app.database.models.wallet import Wallet

    wallet_row = await session.get(Wallet, approved.wallet_id)
    if wallet_row:
        buyer = await UserRepo(session).get_by_id(wallet_row.user_id)
        if buyer and buyer.chat_id:
            try:
                await query.message.bot.send_message(
                    buyer.chat_id,
                    f"✅ Your top-up of {format_minor(approved.amount_minor, wallet_row.currency)} was approved! "
                    f"New balance: {format_minor(wallet_row.balance_minor, wallet_row.currency)}",
                )
            except Exception:  # noqa: BLE001
                pass


@router.callback_query(AdminPaymentCB.filter(F.action == "reject"))
async def reject(query: CallbackQuery, callback_data: AdminPaymentCB, session: AsyncSession, user) -> None:
    try:
        rejected = await wallet_service.reject_topup(
            session, transaction_id=int(callback_data.id), admin_telegram_id=user.telegram_id, reason="Rejected by admin"
        )
    except UserError:
        await query.answer("Already reviewed.", show_alert=True)
        return

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="topup.reject",
        target_type="wallet_transaction",
        target_id=str(rejected.id),
    )
    await query.answer("Rejected.")
    text, markup = await _render(session)
    await query.message.edit_text(text, reply_markup=markup)


