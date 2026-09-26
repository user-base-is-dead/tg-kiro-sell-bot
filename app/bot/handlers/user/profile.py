from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import NavCB
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.common import with_nav
from app.bot.keyboards.styles import PRIMARY, SUCCESS, btn
from app.core.config import get_settings
from app.database.models.user import User
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.locales.i18n import t
from app.utils.money import format_minor

router = Router(name="user.wallet")


async def render_wallet_screen(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup]:
    """👛 Wallet: the one place a buyer sees all of their money.

    Three balances, always all three, because they must never read as one number: the Balance is the
    only one that can buy anything; the Refund Wallet is money from a declined order that staff will
    settle with them; the Frozen Wallet is declined-order money staff are holding for review. Showing
    a $0.00 Frozen Wallet to everybody is deliberate — it is how a buyer learns the wallet exists
    before the day something lands in it.

    A fourth, Sanctioned, appears only while it holds something: Refund Wallet money staff have
    blocked. It is a penalty hold, and a standing "Sanctioned: $0.00" on every buyer's wallet would
    read as a threat rather than as information.

    It replaced 👤 My Account and ➕ Add Funds. Topping up ahead of time is gone; buyers pay at
    checkout, in USDT or from this balance.
    """
    wallet = await WalletRepo(session).get_or_create(user.id, currency=get_settings().default_currency)
    currency = wallet.currency
    locale = user.locale

    sanctioned = ""
    notes = t("wallet_home.notes", locale)
    if wallet.sanctioned_balance_minor:
        sanctioned = "\n" + t(
            "wallet_home.sanctioned", locale, amount=format_minor(wallet.sanctioned_balance_minor, currency)
        )
        notes += "\n" + t("wallet_home.sanctioned_note", locale)

    text = "\n\n".join(
        [
            t("wallet_home.title", locale),
            t(
                "wallet_home.balances",
                locale,
                balance=format_minor(wallet.balance_minor, currency),
                refund=format_minor(wallet.refund_balance_minor, currency),
                frozen=format_minor(wallet.frozen_balance_minor, currency),
                sanctioned=sanctioned,
            ),
            notes,
        ]
    )

    # 📊 Transactions is always drawn: money an admin credits by hand arrives with no announcement,
    # so this is the only way a buyer can ever find out where a balance came from. ↩️ Refunds only
    # when there is something behind it — a door that opens onto "no refunds" for almost everybody
    # teaches people to ignore the row it sits in.
    rows = [[btn(t("menu.transactions", locale), NavCB(target="wallet").pack(), PRIMARY)]]
    held = wallet.refund_balance_minor + wallet.frozen_balance_minor + wallet.sanctioned_balance_minor
    has_history = bool(await OrderRepo(session).list_refunded_for_user(user.id, limit=1))
    if held or has_history:
        label = t("menu.refunds", locale)
        if held:
            label += f" ({format_minor(held, currency)})"
        rows.append([btn(label, NavCB(target="refunds").pack(), SUCCESS)])

    return text, with_nav(rows, locale, back_target="home", home=False)


@router.message(Command("wallet"))
@router.message(MenuButton("menu.wallet"))
async def cmd_wallet(message: Message, session: AsyncSession, user: User) -> None:
    text, markup = await render_wallet_screen(session, user)
    await message.answer(text, reply_markup=markup)
