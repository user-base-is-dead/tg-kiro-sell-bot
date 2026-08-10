from __future__ import annotations

import asyncio
import contextlib

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

# A broadcast is one post, not a newsletter. The cap keeps a slip of the finger from queueing
# fifty messages per user.
MAX_PARTS = 10
TITLE_MAX_CHARS = 60

_PARTS = "parts"
_PREVIEW_IDS = "preview_message_ids"

_WRITE_HEADER = (
    "📢 <b>Broadcast</b>\n\n"
    "Send whatever you want to broadcast — text, photo, video, audio, files, voice notes. "
    "Send as many as you like and they'll go out in the same order.\n\n"
    "Tap <b>Done</b> when you've finished."
)


def _part_label(message: Message) -> str:
    """Short description of a collected part, for the running counter."""
    if message.photo:
        return "🖼️ Photo"
    if message.video:
        return "🎬 Video"
    if message.animation:
        return "🎞️ GIF"
    if message.audio:
        return "🎵 Audio"
    if message.voice:
        return "🎤 Voice"
    if message.video_note:
        return "⭕ Video note"
    if message.document:
        return f"📎 {message.document.file_name or 'File'}"
    if message.sticker:
        return "🌟 Sticker"
    text = (message.text or message.caption or "").strip().replace("\n", " ")
    return f"💬 {text[:40]}…" if len(text) > 40 else f"💬 {text}"


def _derive_title(labels: list[str]) -> str:
    """The internal title is only ever shown to admins, in the broadcast list and
    /broadcast_status, so asking for one separately was a step that earned nothing."""
    first = next((label for label in labels if label.startswith("💬")), None)
    source = (first[2:] if first else (labels[0] if labels else "")).strip().rstrip("…")
    if not source:
        return "Broadcast"
    return source[: TITLE_MAX_CHARS - 1] + "…" if len(source) > TITLE_MAX_CHARS else source


def _writing_keyboard() -> InlineKeyboardMarkup:
    """Done sits beside Abort from the very first screen. It is deliberately shown even with an
    empty draft: the instructions say "tap Done when you've finished", and a button that is
    described but missing reads as a broken screen. Pressing it early answers with a popup."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn("✅ Done", "broadcast_done", SUCCESS),
                btn("❌ Abort", "broadcast_abort", DANGER),
            ]
        ]
    )


def _writing_text(labels: list[str]) -> str:
    if not labels:
        return _WRITE_HEADER
    listed = "\n".join(f"{i}. {label}" for i, label in enumerate(labels, start=1))
    return f"{_WRITE_HEADER}\n\n━━━━━━━━━━━━━━━━━━\n📝 <b>{len(labels)} part(s):</b>\n{listed}"


async def _delete_quietly(bot, chat_id: int, message_ids: list[int]) -> None:
    """Preview copies are throwaway. A delete can fail (already gone, too old) and that must never
    interrupt the flow it is cleaning up after."""
    for message_id in message_ids:
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=chat_id, message_id=message_id)


async def _show_writing_screen(message: Message, state: FSMContext) -> None:
    """Always a *fresh* message rather than an edit. The composer is a sequence of real messages
    (media included), so there is no single message that can be edited back into the writing
    screen — and an edit that silently fails is what made Back look broken."""
    data = await state.get_data()
    labels = [p["label"] for p in data.get(_PARTS, [])]
    await message.answer(_writing_text(labels), reply_markup=_writing_keyboard())


@router.callback_query(AdminMiscCB.filter(F.action == "broadcast"))
async def start_broadcast(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastForm.body)
    await state.update_data(**{_PARTS: [], _PREVIEW_IDS: []})
    await query.message.edit_text(_WRITE_HEADER, reply_markup=_writing_keyboard())
    await query.answer()


@router.message(Command("cancel"), BroadcastForm.body)
async def cancel_broadcast(message: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    await message.answer("❌ Broadcast cancelled.", reply_markup=back_keyboard(user.locale, target="admin_panel"))


@router.message(BroadcastForm.body)
async def collect_part(message: Message, state: FSMContext) -> None:
    """Accepts any message type. Only the coordinates are kept — delivery re-copies the original,
    so a photo stays a photo and a 2 GB file is never re-uploaded."""
    data = await state.get_data()
    parts = list(data.get(_PARTS, []))

    if len(parts) >= MAX_PARTS:
        await message.answer(
            f"⚠️ That's the limit of {MAX_PARTS} parts — tap Done to review what you have."
        )
        return

    parts.append(
        {"chat_id": message.chat.id, "message_id": message.message_id, "label": _part_label(message)}
    )
    await state.update_data(**{_PARTS: parts})
    await _show_writing_screen(message, state)


@router.callback_query(F.data == "broadcast_done", BroadcastForm.body)
async def show_preview(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    parts = data.get(_PARTS, [])
    if not parts:
        await query.answer("Send something to broadcast first.", show_alert=True)
        return

    await query.answer()
    chat_id = query.message.chat.id
    bot = query.message.bot

    # The preview is the real thing: each part copied back to the admin exactly as a user will
    # receive it. Describing media in text could never show a broken caption or a wrong file.
    preview_ids: list[int] = []
    await query.message.edit_text("🔍 <b>Preview</b> — this is exactly what users will receive:")
    for part in parts:
        with contextlib.suppress(Exception):
            copied = await bot.copy_message(
                chat_id=chat_id, from_chat_id=part["chat_id"], message_id=part["message_id"]
            )
            preview_ids.append(copied.message_id)

    control = await query.message.answer(
        "━━━━━━━━━━━━━━━━━━\n"
        f"☝️ {len(parts)} part(s) above.\n\n"
        "<b>Send</b> delivers this to every user.\n"
        "<b>Back</b> discards it and starts over from an empty draft.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    btn("🔙 Back", "broadcast_back", DANGER),
                    btn("🚀 Send", "broadcast_send", SUCCESS),
                ]
            ]
        ),
    )
    preview_ids.append(control.message_id)
    await state.update_data(**{_PREVIEW_IDS: preview_ids})
    await state.set_state(BroadcastForm.confirm)


@router.callback_query(F.data == "broadcast_back", BroadcastForm.confirm)
async def back_to_writing(query: CallbackQuery, state: FSMContext) -> None:
    """Back throws the whole draft away and reopens an empty composer — "wapas start se". The
    preview copies are removed too, so the chat doesn't accumulate the discarded attempt."""
    await query.answer()
    data = await state.get_data()

    await state.set_state(BroadcastForm.body)
    await state.update_data(**{_PARTS: [], _PREVIEW_IDS: []})

    await _delete_quietly(query.message.bot, query.message.chat.id, data.get(_PREVIEW_IDS, []))
    await query.message.answer(_WRITE_HEADER, reply_markup=_writing_keyboard())


@router.callback_query(F.data == "broadcast_send", BroadcastForm.confirm)
async def confirm_send(query: CallbackQuery, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    parts = data.get(_PARTS, [])
    if not parts:
        await query.answer("Nothing to send.", show_alert=True)
        return

    await query.answer()
    labels = [p["label"] for p in parts]
    # Coordinates only — the label is composer bookkeeping and has no meaning at delivery time.
    coordinates = [{"chat_id": p["chat_id"], "message_id": p["message_id"]} for p in parts]
    await state.clear()

    broadcast = await create_broadcast(
        session,
        admin_id=user.telegram_id,
        title=_derive_title(labels),
        body="\n".join(labels),
        parts=coordinates,
    )
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="broadcast.send",
        target_type="broadcast",
        target_id=str(broadcast.id),
        metadata={"total_targets": broadcast.total_targets, "parts": len(parts)},
    )

    # The preview copies stay: they are the record of what was sent. Only the control message is
    # replaced, so the buttons can't be tapped a second time.
    await query.message.edit_text(
        f"🚀 <b>Sending to {broadcast.total_targets} user(s)…</b>\n\n"
        f"Check progress with <code>/broadcast_status {broadcast.id}</code>",
        reply_markup=back_keyboard(user.locale, target="admin_panel"),
    )

    settings = get_settings()
    asyncio.create_task(run_worker(query.message.bot, settings.database_url, broadcast.id))  # noqa: RUF006 — fire-and-forget worker


@router.callback_query(F.data == "broadcast_abort")
async def abort_broadcast(query: CallbackQuery, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    await state.clear()
    await query.answer()
    await _delete_quietly(query.message.bot, query.message.chat.id, data.get(_PREVIEW_IDS, []))
    await query.message.edit_text(
        "❌ Broadcast cancelled.",
        reply_markup=back_keyboard(user.locale, target="admin_panel"),
    )


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
