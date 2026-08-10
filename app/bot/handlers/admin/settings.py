from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminMiscCB, NavCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.common import with_nav
from app.bot.keyboards.styles import PRIMARY, btn
from app.bot.states.settings_form import SettingsForm
from app.core.config import get_settings
from app.database.repositories.audit_repo import AuditRepo
from app.services import settings_service
from app.utils.money import format_minor, parse_to_minor

router = Router(name="admin.settings")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


async def _render(session: AsyncSession, locale: str = "en") -> tuple[str, InlineKeyboardMarkup]:
    reward = await settings_service.get_setting(session, "referral_reward_minor")
    currency = get_settings().default_currency
    text = (
        "⚙️ <b>SETTINGS</b>\n\n"
        "Manage bot-wide configuration and incentive programs.\n\n"
        f"🔗 <b>Referral Reward</b>\n"
        f"Amount credited to a user for each qualified referral.\n"
        f"Current: {format_minor(reward, currency)}\n\n"
        "Use the buttons below to modify settings."
    )
    rows = [
        [btn("✏️ Edit referral reward", "editrefreward", PRIMARY)],
    ]
    markup = with_nav(rows, locale, back_target="admin_panel", home=False)
    return text, markup


@router.callback_query(AdminMiscCB.filter(F.action == "settings"))
async def show_settings(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    text, markup = await _render(session, user.locale)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(F.data == "editrefreward")
async def start_edit(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsForm.referral_reward)
    await query.message.edit_text("Send the new referral reward amount, e.g. 5.00 (or /cancel):")
    await query.answer()


@router.message(Command("cancel"), SettingsForm.referral_reward)
async def cancel_edit(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


@router.message(SettingsForm.referral_reward)
async def set_reward(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    try:
        amount_minor = parse_to_minor((message.text or "").strip())
        if amount_minor < 0:
            raise ValueError
    except ValueError:
        await message.answer("Please send a valid non-negative amount:")
        return

    await settings_service.set_setting(session, "referral_reward_minor", amount_minor, admin_id=user.telegram_id)
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id, action="settings.update", target_type="referral_reward_minor", metadata={"value": amount_minor}
    )
    await state.clear()
    text, markup = await _render(session)
    await message.answer(text, reply_markup=markup)
