from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminMiscCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.common import back_keyboard
from app.bot.keyboards.styles import DANGER, SUCCESS, btn
from app.bot.states.broadcast_form import BroadcastForm
from app.core.config import get_settings
from app.database.models.user import User
from app.database.repositories.audit_repo import AuditRepo
from app.services.broadcast_service import create_broadcast, run_worker

router = Router(name="admin.broadcast")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def _abort_keyboard() -> InlineKeyboardMarkup:
    """Every step of the broadcast form carries this, so a half-filled draft is never a dead end
    the admin can only leave by remembering to type /cancel."""
    return InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Abort", "broadcast_abort", DANGER)]])


@router.callback_query(AdminMiscCB.filter(F.action == "broadcast"))
async def start_broadcast(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastForm.title)
    await query.message.edit_text(
        "📢 <b>Broadcast</b>\n\nSend a short internal title for this broadcast (or /cancel):",
        reply_markup=_abort_keyboard(),
    )
    await query.answer()


@router.message(Command("cancel"), BroadcastForm.title)
@router.message(Command("cancel"), BroadcastForm.body)
async def cancel_broadcast(message: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    await message.answer("❌ Broadcast cancelled.", reply_markup=back_keyboard(user.locale, target="admin_panel"))


@router.message(BroadcastForm.title)
async def set_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("Please send a short title:", reply_markup=_abort_keyboard())
        return
    await state.update_data(title=title)
    await state.set_state(BroadcastForm.body)
    await message.answer(
        "Now send the message body (HTML formatting supported):",
        reply_markup=_abort_keyboard(),
    )


@router.message(BroadcastForm.body)
async def set_body(message: Message, state: FSMContext) -> None:
    body = message.html_text or message.text or ""
    if not body:
        await message.answer("Please send some text:", reply_markup=_abort_keyboard())
        return
    await state.update_data(body=body)
    await state.set_state(BroadcastForm.confirm)

    markup = InlineKeyboardMarkup(inline_keyboard=[[
        btn("✅ Done", "broadcast_send", SUCCESS),
        btn("❌ Abort", "broadcast_abort", DANGER),
    ]])
    await message.answer(f"<b>Preview:</b>\n\n{body}\n\n— tap Done to send or Abort to cancel.", reply_markup=markup)


@router.callback_query(F.data == "broadcast_send", BroadcastForm.confirm)
async def confirm_send(query: CallbackQuery, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    await state.clear()

    broadcast = await create_broadcast(session, admin_id=user.telegram_id, title=data["title"], body=data["body"])
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="broadcast.send",
        target_type="broadcast",
        target_id=str(broadcast.id),
        metadata={"total_targets": broadcast.total_targets},
    )
    await query.message.edit_text(f"🚀 Sending to {broadcast.total_targets} user(s)... use /broadcast_status {broadcast.id} to check progress.")
    await query.answer()

    settings = get_settings()
    asyncio.create_task(run_worker(query.message.bot, settings.database_url, broadcast.id))  # noqa: RUF006 — fire-and-forget worker


@router.callback_query(F.data == "broadcast_abort")
async def abort_broadcast(query: CallbackQuery, state: FSMContext, user: User) -> None:
    await state.clear()
    await query.message.edit_text(
        "❌ Broadcast cancelled.",
        reply_markup=back_keyboard(user.locale, target="admin_panel"),
    )
    await query.answer()


@router.message(Command("broadcast_status"))
async def broadcast_status(message: Message, command: CommandObject, session: AsyncSession) -> None:
    from app.database.models.broadcast import Broadcast

    if not command.args or not command.args.strip().isdigit():
        await message.answer("Usage: /broadcast_status <id>")
        return

    broadcast = await session.get(Broadcast, int(command.args.strip()))
    if broadcast is None:
        await message.answer("Not found.")
        return

    await message.answer(
        f"📢 <b>{broadcast.title}</b>\n\n"
        f"Status: {broadcast.status.value}\n"
        f"Targets: {broadcast.total_targets}\n"
        f"Sent: {broadcast.sent_count}\n"
        f"Failed/Blocked: {broadcast.failed_count}"
    )
