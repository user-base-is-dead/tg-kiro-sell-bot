from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import NavCB
from app.bot.filters.is_admin import is_admin_user
from app.bot.filters.menu_button import MenuButton
from app.bot.keyboards.common import back_keyboard, with_nav
from app.bot.keyboards.main_menu import main_inline_keyboard
from app.bot.keyboards.styles import PRIMARY, btn
from app.bot.states.gift_form import GiftRedeemForm
from app.bot.texts import NO_PREVIEW, home_body
from app.database.models.gift import GiftCode, GiftKind
from app.database.models.user import User
from app.locales.i18n import t
from app.services.gift_service import ClaimedGift, get_active_gift, redeem_gift_by_id, redeem_gift_code
from app.utils.errors import UserError
from app.utils.money import format_minor

router = Router(name="gifts.redeem")



async def _offer_screen(session: AsyncSession, gift: GiftCode, locale: str) -> tuple[str, InlineKeyboardMarkup]:
    """The claim screen. It spells out what the gift actually is, because a product gift and a
    credit gift are worth very different things and the description alone may not say which."""
    if gift.kind is GiftKind.ITEM:
        grant = t("gift.grants_item", locale)
    else:
        grant = t("gift.grants_credit", locale, amount=format_minor(gift.value_minor or 0, gift.currency))

    description = gift.description or t("gift.default_description", locale)
    text = f"🎁 <b>Free Gift</b>\n\n{description}\n\n{grant}"
    # with_nav supplies the standard red Back going to "home" — the only nav target the router
    # actually resolves. Hand-rolling this row is what previously produced an unstyled button
    # pointing at a target nav.py has never handled.
    markup = with_nav(
        [[btn(t("gift.claim", locale), NavCB(target="claim_gift").pack(), PRIMARY)]],
        locale,
        back_target="home",
        home=False,
    )
    return text, markup


async def _claimed_text(session: AsyncSession, claimed: ClaimedGift, locale: str) -> str:
    if claimed.gift.kind is not GiftKind.ITEM:
        return t("gift.redeemed", locale, amount=format_minor(claimed.gift.value_minor or 0, claimed.gift.currency))

    # A gift item is handed over directly. It is not a catalog product, so there is no order, no
    # warranty and no delivery note — those belong to things people pay for.
    return t("gift.redeemed_item", locale, payload=claimed.delivered_payload or "—")


@router.message(Command("gift"))
@router.message(MenuButton("menu.gift"))
async def cmd_gift(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    gift = await get_active_gift(session)
    if not gift:
        await message.answer(t("gift.invalid_code", user.locale), reply_markup=back_keyboard(user.locale))
        return

    text, markup = await _offer_screen(session, gift, user.locale)
    await state.set_state(GiftRedeemForm.code)
    await message.answer(text, reply_markup=markup)


@router.callback_query(NavCB.filter(F.target == "gift"))
async def nav_gift(query: CallbackQuery, callback_data: NavCB, state: FSMContext, session: AsyncSession, user: User) -> None:  # noqa: ARG001
    if not query.message:
        return
    await state.clear()
    gift = await get_active_gift(session)
    if not gift:
        await query.message.edit_text(
            t("gift.invalid_code", user.locale), reply_markup=back_keyboard(user.locale)
        )
        await query.answer()
        return

    text, markup = await _offer_screen(session, gift, user.locale)
    await state.set_state(GiftRedeemForm.code)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(NavCB.filter(F.target == "claim_gift"))
async def claim_gift(query: CallbackQuery, callback_data: NavCB, session: AsyncSession, user: User) -> None:  # noqa: ARG001
    if not query.message:
        return

    gift = await get_active_gift(session)
    if not gift:
        await query.answer(t("gift.none_available_toast", user.locale), show_alert=True)
        return

    try:
        claimed = await redeem_gift_by_id(session, user_id=user.id, gift_id=gift.id)
    except UserError as exc:
        await query.answer(t(exc.i18n_key, user.locale), show_alert=True)
        return

    await query.message.edit_text(
        await _claimed_text(session, claimed, user.locale),
        reply_markup=back_keyboard(user.locale),
    )
    await query.answer(t("gift.claimed_toast", user.locale))


@router.message(Command("cancel"), GiftRedeemForm.code)
async def cancel_gift(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    is_admin = await is_admin_user(session, user.telegram_id)
    await message.answer(
        home_body(user.locale, user.first_name),
        reply_markup=main_inline_keyboard(user.locale, is_admin=is_admin),
        link_preview_options=NO_PREVIEW,
    )


@router.message(GiftRedeemForm.code)
async def receive_code(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    code = (message.text or "").strip()
    await state.clear()

    try:
        claimed = await redeem_gift_code(session, user_id=user.id, code_plaintext=code)
    except UserError as exc:
        await message.answer(t(exc.i18n_key, user.locale), reply_markup=back_keyboard(user.locale))
        return

    await message.answer(
        await _claimed_text(session, claimed, user.locale),
        reply_markup=back_keyboard(user.locale),
    )
