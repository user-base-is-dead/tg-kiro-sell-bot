from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import NavCB
from app.bot.keyboards.common import with_nav
from app.bot.keyboards.styles import NEUTRAL, PRIMARY, btn
from app.core.config import get_settings
from app.database.models.user import User
from app.database.models.wallet import TxnType, WalletTransaction
from app.database.repositories.wallet_repo import WalletRepo
from app.locales.i18n import t
from app.utils.money import format_minor
from app.utils.pagination import Page
from app.utils.text import escape_html
from app.utils.time import as_utc

router = Router(name="user.wallet_history")

PAGE_SIZE = 10

# What each ledger row means to the person whose money it is. A buyer should not have to know what
# ADMIN_ADJUST or REFUND_MOVE is to read their own wallet, which is the same reason `refunds.py`
# keeps its own map for the refund side.
#
# ADMIN_ADJUST is the only type that goes both ways, so it is resolved by sign rather than by type:
# `credit()` stores a positive amount and `debit()` a negative one.
_ROW_LABEL = {
    TxnType.TOPUP: "row_topup",
    TxnType.PURCHASE: "row_purchase",
    TxnType.REFUND: "row_refund",
    TxnType.REFUND_MOVE: "row_refund_moved",
    TxnType.GIFT: "row_gift",
    TxnType.REFERRAL: "row_referral",
}


def _row_key(txn: WalletTransaction) -> str | None:
    if txn.type is TxnType.ADMIN_ADJUST:
        return "row_admin_credit" if txn.amount_minor >= 0 else "row_admin_debit"
    return _ROW_LABEL.get(txn.type)


def _row_note(txn: WalletTransaction) -> str | None:
    """The reason an admin typed, when there is one.

    `admin_adjust` files it in `ref_id`, so "our team added $10" can say why without a schema change.
    Nothing else on the MAIN ledger puts human text there, hence the `ref_type` guard — a stray
    order id or crypto payment id printed as a reason would read as gibberish.
    """
    if txn.ref_type != "admin_adjust" or not txn.ref_id:
        return None
    return escape_html(txn.ref_id.strip()) or None


def _keyboard(page: Page, locale: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if page.total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page.has_prev:
            nav.append(btn("◀️", NavCB(target=f"wallet-{page.clamped_page - 1}").pack(), PRIMARY))
        nav.append(btn(f"{page.clamped_page}/{page.total_pages}", "noop", NEUTRAL))
        if page.has_next:
            nav.append(btn("▶️", NavCB(target=f"wallet-{page.clamped_page + 1}").pack(), PRIMARY))
        rows.append(nav)
    return with_nav(rows, locale, back_target="profile", home=True)


async def render_wallet_history(
    session: AsyncSession, user: User, page_num: int = 1
) -> tuple[str, InlineKeyboardMarkup]:
    """The statement behind the balance on the profile.

    Every movement of a user's money was already written to `wallet_transactions` and none of it was
    ever shown to them: the profile gave a single number and 💸 My Refunds covered only the refund
    account. So money an admin credited by hand, or moved back out of the Refund Wallet, simply
    appeared — with nothing on any screen saying where it came from, and no way to check it had
    arrived short of remembering what the number used to be.
    """
    locale = user.locale
    wallet = await WalletRepo(session).get_or_create(user.id, currency=get_settings().default_currency)
    repo = WalletRepo(session)

    total = await repo.count_main_transactions(wallet.id)
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=total)
    txns = await repo.list_main_transactions(wallet.id, limit=PAGE_SIZE, offset=page.offset)

    lines = [t("wallet.title", locale), ""]
    lines.append(t("wallet.balance", locale, amount=format_minor(wallet.balance_minor, wallet.currency)))
    # Stated here too, because a buyer looking for money they were told about should not have to
    # guess which of the two screens it landed on. It is deliberately labelled as not spendable.
    if wallet.refund_balance_minor:
        lines.append(
            t("wallet.held", locale, amount=format_minor(wallet.refund_balance_minor, wallet.currency))
        )
    lines.append("")

    if not txns:
        lines.append(t("wallet.empty", locale))
        return "\n".join(lines), _keyboard(page, locale)

    lines.append(t("wallet.history_heading", locale))
    for txn in txns:
        key = _row_key(txn)
        if key is None:
            # An unmapped type is a new one nobody has written a label for yet. Skipping it silently
            # would make money vanish from a statement, so it falls back to the amount alone.
            body = t(
                "wallet.row_other",
                locale,
                amount=format_minor(abs(txn.amount_minor), wallet.currency),
            )
        else:
            body = t(
                f"wallet.{key}",
                locale,
                amount=format_minor(abs(txn.amount_minor), wallet.currency),
            )
        lines.append(f"<code>{as_utc(txn.created_at):%d %b}</code> {body}")
        note = _row_note(txn)
        if note:
            lines.append(f"   <i>{note}</i>")

    lines.append("")
    lines.append(t("wallet.footer", locale))
    return "\n".join(lines), _keyboard(page, locale)


@router.message(Command("wallet"))
async def cmd_wallet(message: Message, session: AsyncSession, user: User) -> None:
    text, markup = await render_wallet_history(session, user)
    await message.answer(text, reply_markup=markup)
