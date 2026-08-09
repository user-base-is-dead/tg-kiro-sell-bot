from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import NavCB
from app.bot.filters.is_admin import is_admin_user
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.common import back_keyboard
from app.bot.keyboards.main_menu import main_inline_keyboard
from app.bot.states.gift_form import GiftRedeemForm
from app.database.models.user import User
from app.locales.i18n import t
from app.services.gift_service import redeem_gift_code
from app.utils.errors import UserError
from app.utils.money import format_minor

router = Router(name="gifts.redeem")


@router.message(Command("gift"))
@router.message(MenuButton("menu.gift"))
async def cmd_gift(message: Message, state: FSMContext, user: User) -> None:
    await state.set_state(GiftRedeemForm.code)
    await message.answer(t("gift.prompt", user.locale), reply_markup=back_keyboard(user.locale))


@router.callback_query(NavCB.filter(F.target == "gift"))
async def nav_gift(query: CallbackQuery, callback_data: NavCB, state: FSMContext, user: User) -> None:  # noqa: ARG001
    if not query.message:
        return
    await state.set_state(GiftRedeemForm.code)
    await query.message.edit_text(t("gift.prompt", user.locale), reply_markup=back_keyboard(user.locale))
    await query.answer()


@router.message(Command("cancel"), GiftRedeemForm.code)
async def cancel_gift(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    is_admin = await is_admin_user(session, user.telegram_id)
    await message.answer(
        t("welcome.subtitle", user.locale, name=user.first_name or "there"),
        reply_markup=main_inline_keyboard(user.locale, is_admin=is_admin),
    )


@router.message(GiftRedeemForm.code)
async def receive_code(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    code = (message.text or "").strip()
    await state.clear()

    try:
        gift = await redeem_gift_code(session, user_id=user.id, code_plaintext=code)
    except UserError as exc:
        await message.answer(t(exc.i18n_key, user.locale), reply_markup=back_keyboard(user.locale))
        return

    await message.answer(
        t("gift.redeemed", user.locale, amount=format_minor(gift.value_minor, gift.currency)),
        reply_markup=back_keyboard(user.locale),
    )
