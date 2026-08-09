from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminGiftCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.styles import NEUTRAL, PRIMARY, btn
from app.bot.states.gift_form import GiftCreateForm
from app.core.config import get_settings
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.gift_repo import GiftRepo
from app.services.gift_service import create_gift_code
from app.utils.money import format_minor, parse_to_minor

router = Router(name="admin.gifts")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def _list_keyboard(gifts: list) -> InlineKeyboardMarkup:
    rows = [
        [
            btn(
                f"{'🟢' if g.status.value == 'ACTIVE' else '⚫'} ****{g.code_last4} — {format_minor(g.value_minor, g.currency)} ({g.used_count}/{g.max_uses})",
                AdminGiftCB(action="view", id=str(g.id)).pack(),
                PRIMARY if g.status.value == "ACTIVE" else NEUTRAL,
            )
        ]
        for g in gifts
    ]
    rows.append([btn("➕ Create Gift Code", AdminGiftCB(action="add").pack(), PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AdminGiftCB.filter(F.action == "list"))
async def list_gifts(query: CallbackQuery, session: AsyncSession) -> None:
    gifts = await GiftRepo(session).list_all()
    await query.message.edit_text(
        f"🎁 <b>GIFT CODES</b>\n\n{len(gifts)} code(s).", reply_markup=_list_keyboard(gifts)
    )
    await query.answer()


@router.callback_query(AdminGiftCB.filter(F.action == "view"))
async def view_gift(query: CallbackQuery, callback_data: AdminGiftCB, session: AsyncSession) -> None:
    gift = await GiftRepo(session).get_by_id(int(callback_data.id))
    if gift is None:
        await query.answer("Not found.", show_alert=True)
        return
    expires_line = f"Expires: {gift.expires_at:%d %b %Y}\n" if gift.expires_at else "Expires: never\n"
    text = (
        f"🎁 <b>****{gift.code_last4}</b>\n\n"
        f"Value: {format_minor(gift.value_minor, gift.currency)}\n"
        f"Uses: {gift.used_count}/{gift.max_uses}\n"
        f"Per-user limit: {gift.per_user_limit}\n"
        f"{expires_line}"
        f"Status: {gift.status.value}"
    )
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[btn("🔙 Back", AdminGiftCB(action="list").pack(), PRIMARY)]]
        ),
    )
    await query.answer()


# ---- Create Gift wizard ----


@router.callback_query(AdminGiftCB.filter(F.action == "add"))
async def start_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GiftCreateForm.value)
    await query.message.edit_text("➕ <b>Create Gift Code</b>\n\nSend the value, e.g. 10.00 (or /cancel):")
    await query.answer()


@router.message(Command("cancel"), GiftCreateForm.value)
@router.message(Command("cancel"), GiftCreateForm.max_uses)
@router.message(Command("cancel"), GiftCreateForm.per_user_limit)
@router.message(Command("cancel"), GiftCreateForm.expires_days)
async def cancel_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


@router.message(GiftCreateForm.value)
async def set_value(message: Message, state: FSMContext) -> None:
    try:
        value_minor = parse_to_minor((message.text or "").strip())
        if value_minor <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Please send a valid positive amount:")
        return
    await state.update_data(value_minor=value_minor)
    await state.set_state(GiftCreateForm.max_uses)
    await message.answer("How many total redemptions allowed? (e.g. 1, 50, 1000):")


@router.message(GiftCreateForm.max_uses)
async def set_max_uses(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Please send a positive whole number:")
        return
    await state.update_data(max_uses=int(text))
    await state.set_state(GiftCreateForm.per_user_limit)
    await message.answer("Per-user redemption limit (usually 1):")


@router.message(GiftCreateForm.per_user_limit)
async def set_per_user_limit(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Please send a positive whole number:")
        return
    await state.update_data(per_user_limit=int(text))
    await state.set_state(GiftCreateForm.expires_days)
    await message.answer("Expires in how many days? (0 = never expires):")


@router.message(GiftCreateForm.expires_days)
async def set_expires_days(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Please send a whole number of days (0 for never):")
        return
    days = int(text)
    expires_at = datetime.now(UTC) + timedelta(days=days) if days > 0 else None

    data = await state.get_data()
    plaintext_code = await create_gift_code(
        session,
        value_minor=data["value_minor"],
        currency=get_settings().default_currency,
        max_uses=data["max_uses"],
        per_user_limit=data["per_user_limit"],
        expires_at=expires_at,
        admin_id=user.telegram_id,
    )
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="gift.create",
        target_type="gift_code",
        metadata={"value_minor": data["value_minor"], "max_uses": data["max_uses"]},
    )
    await state.clear()
    await message.answer(
        f"✅ Gift code created:\n\n<code>{plaintext_code}</code>\n\n"
        f"⚠️ This code is shown once — save it now. Only the last 4 characters are stored."
    )


