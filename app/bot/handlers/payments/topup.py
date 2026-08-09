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
from app.bot.states.topup_form import TopUpForm
from app.core.config import get_settings
from app.database.models.user import User
from app.database.repositories.wallet_repo import WalletRepo
from app.locales.i18n import t
from app.services.payments.registry import get_provider
from app.utils.money import format_minor, parse_to_minor

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
    wallet = await WalletRepo(session).get_or_create(user.id, currency=get_settings().default_currency)
    packages_text, markup = await render_topup_packages(user.locale, show_title=False)
    text = (
        f"💰 <b>Top Up Wallet - Crypto</b>\n\n"
        f"Current balance: {format_minor(wallet.balance_minor, wallet.currency)}\n\n"
        + packages_text
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.message(Command("cancel"), TopUpForm.amount)
@router.message(Command("cancel"), TopUpForm.proof)
async def cancel_topup(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    is_admin = await is_admin_user(session, user.telegram_id)
    await message.answer(
        t("welcome.subtitle", user.locale, name=user.first_name or "there"),
        reply_markup=main_inline_keyboard(user.locale, is_admin=is_admin),
    )


@router.message(TopUpForm.amount)
async def set_amount(message: Message, state: FSMContext, user: User) -> None:
    try:
        amount_minor = parse_to_minor((message.text or "").strip())
        if amount_minor <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Please send a valid positive amount, e.g. 20.00:")
        return
    await state.update_data(amount_minor=amount_minor)
    await state.set_state(TopUpForm.proof)
    await message.answer(
        "Send proof of payment (a screenshot, transaction id, or note for the admin reviewing this):",
        reply_markup=back_keyboard(user.locale),
    )


@router.message(TopUpForm.proof)
async def set_proof(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    proof = message.text or (message.caption or "") or (message.photo[-1].file_id if message.photo else "")
    if not proof:
        await message.answer("Please send some text describing your payment, or /cancel:")
        return

    data = await state.get_data()
    provider = get_provider()
    intent = await provider.create_intent(
        session,
        user_id=user.id,
        amount_minor=data["amount_minor"],
        currency=get_settings().default_currency,
        proof=proof[:512],
    )
    await state.clear()
    await message.answer(
        f"✅ Requested {format_minor(intent.amount_minor, intent.currency)} top-up.\n\n"
        + provider.render_instructions(intent),
        reply_markup=back_keyboard(user.locale),
    )
