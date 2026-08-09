from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminPaymentCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.styles import NEUTRAL, PRIMARY, btn
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services import wallet_service
from app.utils.errors import UserError
from app.utils.money import format_minor

router = Router(name="admin.payments")
router.callback_query.filter(IsAdmin())


def _list_keyboard(txns: list) -> InlineKeyboardMarkup:
    # Approve/reject sit side by side on every row â€” green vs red is what stops a mis-tap here.
    rows = [
        [
            btn(
                f"âœ… #{t.id} {format_minor(t.amount_minor, 'USD')}",
                AdminPaymentCB(action="approve", id=str(t.id)).pack(),
                PRIMARY,
            ),
            btn("âŒ Reject", AdminPaymentCB(action="reject", id=str(t.id)).pack(), PRIMARY),
        ]
        for t in txns
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[btn("â€” none â€”", "noop", NEUTRAL)]])


@router.callback_query(AdminPaymentCB.filter(F.action == "list"))
async def list_pending(query: CallbackQuery, session: AsyncSession) -> None:
    txns = await WalletRepo(session).list_pending_topups()
    text = f"ðŸ’° <b>PENDING TOP-UPS</b>\n\n{len(txns)} request(s) awaiting review."
    await query.message.edit_text(text, reply_markup=_list_keyboard(txns))
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
    txns = await WalletRepo(session).list_pending_topups()
    await query.message.edit_text(
        f"ðŸ’° <b>PENDING TOP-UPS</b>\n\n{len(txns)} request(s) awaiting review.", reply_markup=_list_keyboard(txns)
    )

    # Notify the buyer directly (best-effort â€” they may have blocked the bot).
    from app.database.models.wallet import Wallet

    wallet_row = await session.get(Wallet, approved.wallet_id)
    if wallet_row:
        buyer = await UserRepo(session).get_by_id(wallet_row.user_id)
        if buyer and buyer.chat_id:
            try:
                await query.message.bot.send_message(
                    buyer.chat_id,
                    f"âœ… Your top-up of {format_minor(approved.amount_minor, wallet_row.currency)} was approved! "
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
    txns = await WalletRepo(session).list_pending_topups()
    await query.message.edit_text(
        f"ðŸ’° <b>PENDING TOP-UPS</b>\n\n{len(txns)} request(s) awaiting review.", reply_markup=_list_keyboard(txns)
    )


