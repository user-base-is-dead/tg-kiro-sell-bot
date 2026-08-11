from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import NavCB
from app.bot.filters.is_admin import is_admin_user
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.main_menu import main_inline_keyboard
from app.bot.states.topup_form import TopUpForm
from app.bot.texts import NO_PREVIEW, home_body
from app.core.config import get_settings
from app.database.models.user import User
from app.database.repositories.wallet_repo import WalletRepo
from app.utils.money import format_minor

router = Router(name="payments.topup")


@router.message(Command("topup"))
@router.message(MenuButton("menu.topup"))
async def cmd_topup(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    from app.bot.handlers.payments.topup_crypto import render_topup_packages

    wallet = await WalletRepo(session).get_or_create(user.id, currency=get_settings().default_currency)
    packages_text, markup = await render_topup_packages(user.locale, show_title=False)
    text = (
        f"💰 <b>Top Up Wallet - Crypto</b>\n\n"
        f"Current balance: {format_minor(wallet.balance_minor, wallet.currency)}\n\n"
        + packages_text
    )
    await message.answer(text, reply_markup=markup)


@router.callback_query(NavCB.filter(F.target == "topup"))
async def nav_topup(
    query: CallbackQuery, callback_data: NavCB, state: FSMContext, session: AsyncSession, user: User
) -> None:  # noqa: ARG001
    from app.bot.handlers.payments.topup_crypto import render_topup_packages

    if not query.message:
        return
    # Leaving the custom-amount screen abandons the form, so the state must go with it.
    await state.clear()
    wallet = await WalletRepo(session).get_or_create(user.id, currency=get_settings().default_currency)
    packages_text, markup = await render_topup_packages(user.locale, show_title=False)
    text = (
        f"💰 <b>Top Up Wallet - Crypto</b>\n\n"
        f"Current balance: {format_minor(wallet.balance_minor, wallet.currency)}\n\n"
        + packages_text
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


# The amount itself is handled by the crypto flow in `topup_crypto.py`; this module only owns the
# entry screen and /cancel. A second `TopUpForm.amount` handler here would shadow it, since this
# router is registered first.
@router.message(Command("cancel"), TopUpForm.amount)
async def cancel_topup(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    is_admin = await is_admin_user(session, user.telegram_id)
    await message.answer(
        home_body(user.locale, user.first_name),
        reply_markup=main_inline_keyboard(user.locale, is_admin=is_admin),
        link_preview_options=NO_PREVIEW,
    )
