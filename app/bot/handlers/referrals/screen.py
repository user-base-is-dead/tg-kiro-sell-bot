from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import NavCB
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.common import back_keyboard
from app.core.config import get_settings
from app.database.models.user import User
from app.database.repositories.referral_repo import ReferralRepo
from app.locales.i18n import t
from app.services import settings_service
from app.utils.money import format_minor

router = Router(name="referrals.screen")


@router.message(Command("refer"))
@router.message(MenuButton("menu.refer"))
async def cmd_refer(message: Message, session: AsyncSession, user: User) -> None:
    bot_user = await message.bot.get_me()
    link = f"https://t.me/{bot_user.username}?start=ref_{user.referral_code}"

    repo = ReferralRepo(session)
    total = await repo.count_for_referrer(user.id)
    qualified = await repo.count_qualified_for_referrer(user.id)
    reward_minor = await settings_service.get_setting(session, "referral_reward_minor")

    text = t("referral.screen_title", user.locale) + "\n\n" + t(
        "referral.screen_body",
        user.locale,
        link=link,
        total=total,
        qualified=qualified,
        reward=format_minor(reward_minor, get_settings().default_currency),
    )
    await message.answer(text, reply_markup=back_keyboard(user.locale))


@router.callback_query(NavCB.filter(F.target == "refer"))
async def nav_refer(query: CallbackQuery, callback_data: NavCB, session: AsyncSession, user: User) -> None:  # noqa: ARG001
    if not query.message:
        return
    bot_user = await query.message.bot.get_me()
    link = f"https://t.me/{bot_user.username}?start=ref_{user.referral_code}"

    repo = ReferralRepo(session)
    total = await repo.count_for_referrer(user.id)
    qualified = await repo.count_qualified_for_referrer(user.id)
    reward_minor = await settings_service.get_setting(session, "referral_reward_minor")

    text = t("referral.screen_title", user.locale) + "\n\n" + t(
        "referral.screen_body",
        user.locale,
        link=link,
        total=total,
        qualified=qualified,
        reward=format_minor(reward_minor, get_settings().default_currency),
    )
    await query.message.edit_text(text, reply_markup=back_keyboard(user.locale))
    await query.answer()
