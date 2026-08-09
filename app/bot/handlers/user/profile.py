from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters.menu_button import MenuButton
from app.core.config import get_settings
from app.database.models.order import OrderStatus
from app.database.models.user import User
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.utils.money import format_minor

router = Router(name="user.profile")


@router.message(Command("profile"))
@router.message(MenuButton("menu.profile"))
async def cmd_profile(message: Message, session: AsyncSession, user: User) -> None:
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
    await message.answer(text)
