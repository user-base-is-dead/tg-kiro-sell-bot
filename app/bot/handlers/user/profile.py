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
from app.database.models.order import OrderStatus
from app.database.models.user import User
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.locales.i18n import t
from app.utils.money import format_minor

router = Router(name="user.profile")


# One copy, used by both /profile and the 👤 Profile button in the nav router. It existed as two
# verbatim paste-ups, which is how the Refund Balance line ended up on one screen and not the other.
async def render_profile(session: AsyncSession, user: User) -> str:
    wallet = await WalletRepo(session).get_or_create(user.id, currency=get_settings().default_currency)
    order_repo = OrderRepo(session)
    total_orders = await order_repo.count_for_user(user.id)

    recent = await order_repo.list_for_user(user.id, offset=0, limit=100)
    total_spent = sum(o.total_minor for o in recent if o.status == OrderStatus.COMPLETED)

    username = f"@{user.username}" if user.username else "—"
    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>MY PROFILE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"Username: {username}\n"
        f"ID: <code>{user.telegram_id}</code>\n\n"
        f"📦 Orders: {total_orders}\n"
        f"💰 Total Spent: {format_minor(total_spent, wallet.currency)}\n"
        f"💳 Balance: {format_minor(wallet.balance_minor, wallet.currency)}"
    )
    # Only when there is something in it. Money owed back on a declined order is stated as held rather
    # than as part of the balance, because the two must never read as one number — this one cannot buy
    # anything until an admin settles it.
    if wallet.refund_balance_minor:
        text += (
            f"\n💸 Refund Balance: {format_minor(wallet.refund_balance_minor, wallet.currency)}"
            "\n     <i>held for you while we settle it — not spendable</i>"
        )
    return text


async def render_profile_screen(
    session: AsyncSession, user: User
) -> tuple[str, InlineKeyboardMarkup]:
    """The profile plus its keyboard, so the 💸 My Refunds door is on both entry points.

    The button is only drawn for someone who has a refund to look at — either money still held or a
    past one that was settled. A permanent button that opens "you have no refunds" for almost
    everybody teaches people to ignore the row it sits in.
    """
    wallet = await WalletRepo(session).get_or_create(user.id, currency=get_settings().default_currency)
    has_history = bool(await OrderRepo(session).list_refunded_for_user(user.id, limit=1))

    rows = []
    # Always drawn, unlike 💸 My Refunds below. The balance on this screen is a bare number, and the
    # door to what is behind it has to be there before the user has a reason to look for it — money
    # an admin credits by hand arrives with no announcement, so this button is the only way they can
    # ever find out where it came from.
    rows.append([btn(t("menu.wallet", user.locale), NavCB(target="wallet").pack(), PRIMARY)])
    if wallet.refund_balance_minor or has_history:
        label = t("menu.refunds", user.locale)
        if wallet.refund_balance_minor:
            label += f" ({format_minor(wallet.refund_balance_minor, wallet.currency)})"
        rows.append([btn(label, NavCB(target="refunds").pack(), SUCCESS)])

    return await render_profile(session, user), with_nav(rows, user.locale, back_target="home", home=False)


@router.message(Command("profile"))
@router.message(MenuButton("menu.profile"))
async def cmd_profile(message: Message, session: AsyncSession, user: User) -> None:
    text, markup = await render_profile_screen(session, user)
    await message.answer(text, reply_markup=markup)
