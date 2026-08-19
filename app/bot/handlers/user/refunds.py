from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.common import with_nav
from app.core.config import get_settings
from app.database.models.order import FundingSource, RefundState
from app.database.models.user import User
from app.database.models.wallet import TxnType
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.support_repo import SupportRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.locales.i18n import t
from app.utils.money import format_minor
from app.utils.text import escape_html
from app.utils.time import as_utc

router = Router(name="user.refunds")

# What each ledger row means to the person the money belongs to. The admin screen shows the raw
# transaction type; a buyer should not have to know what REFUND_PARK is to read their own history.
_LEDGER_LABEL = {
    TxnType.REFUND_PARK: "refund_row_parked",
    TxnType.REFUND_PAYOUT: "refund_row_sent",
    TxnType.REFUND_MOVE: "refund_row_moved",
    TxnType.REFUND_ADJUST: "refund_row_adjusted",
}


async def render_refunds(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup]:
    """The buyer's own view of money owed back to them.

    Exists because the only thing they could previously see was a single line on their profile
    saying a number was held. That answers "how much" and none of the questions that actually
    follow it: which order, why, what happens next, and whether anything has been paid yet. A
    refund the buyer cannot inspect is one they have to open a ticket to ask about.
    """
    wallet = await WalletRepo(session).get_or_create(user.id, currency=get_settings().default_currency)
    orders = await OrderRepo(session).list_refunded_for_user(user.id, limit=10)
    ledger = await WalletRepo(session).list_refund_transactions(wallet.id, limit=10)

    locale = user.locale
    lines = [t("refunds.title", locale), ""]

    if not orders and wallet.refund_balance_minor <= 0:
        lines.append(t("refunds.empty", locale))
        return "\n".join(lines), with_nav([], locale, back_target="profile", home=True)

    lines.append(
        t("refunds.held", locale, amount=format_minor(wallet.refund_balance_minor, wallet.currency))
    )
    lines.append(
        t("refunds.spendable", locale, amount=format_minor(wallet.balance_minor, wallet.currency))
    )
    lines.append("")

    pending = [o for o in orders if o.refund_state is RefundState.PARKED]
    settled = [o for o in orders if o.refund_state is RefundState.SETTLED]

    if pending:
        lines.append(t("refunds.pending_heading", locale))
        for order in pending:
            when = f"{as_utc(order.cancelled_at):%d %b %Y}" if order.cancelled_at else "—"
            lines.append(
                f"• <code>{order.order_number}</code> — "
                f"<b>{format_minor(order.refund_amount_minor or 0, order.currency)}</b> · {when}"
            )
            if order.failure_reason:
                lines.append(f"   {escape_html(order.failure_reason)}")
            if order.funding_source is FundingSource.CRYPTO:
                lines.append(f"   {t('refunds.crypto_hint', locale)}")
            if order.refund_ticket_id:
                ticket = await SupportRepo(session).get_by_id(order.refund_ticket_id)
                if ticket is not None:
                    lines.append(
                        f"   {t('refunds.ticket', locale, ticket_number=ticket.ticket_number)}"
                    )
        lines.append("")

    if settled:
        lines.append(t("refunds.settled_heading", locale))
        for order in settled:
            lines.append(
                f"• <code>{order.order_number}</code> — "
                f"{format_minor(order.refund_amount_minor or 0, order.currency)} ✅"
            )
        lines.append("")

    if ledger:
        lines.append(t("refunds.ledger_heading", locale))
        for txn in ledger:
            key = _LEDGER_LABEL.get(txn.type)
            if key is None:
                continue
            lines.append(
                f"<code>{as_utc(txn.created_at):%d %b}</code> "
                f"{t(f'refunds.{key}', locale, amount=format_minor(abs(txn.amount_minor), wallet.currency))}"
            )
        lines.append("")

    lines.append(
        t("refunds.footer_pending", locale) if wallet.refund_balance_minor > 0
        else t("refunds.footer_clear", locale)
    )

    return "\n".join(lines), with_nav([], locale, back_target="profile", home=True)


@router.message(Command("refunds"))
async def cmd_refunds(message: Message, session: AsyncSession, user: User) -> None:
    text, markup = await render_refunds(session, user)
    await message.answer(text, reply_markup=markup)
