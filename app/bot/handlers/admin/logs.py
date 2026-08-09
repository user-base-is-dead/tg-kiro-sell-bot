from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminMiscCB
from app.bot.filters.is_admin import IsAdmin
from app.database.repositories.audit_repo import AuditRepo

router = Router(name="admin.logs")
router.callback_query.filter(IsAdmin())


@router.callback_query(AdminMiscCB.filter(F.action == "logs"))
async def show_logs(query: CallbackQuery, session: AsyncSession) -> None:
    entries = await AuditRepo(session).list_recent(20)
    if not entries:
        await query.message.edit_text("📝 <b>LOGS</b>\n\nNo audit entries yet.")
        await query.answer()
        return

    lines = ["📝 <b>LOGS</b> (most recent 20)\n"]
    for e in entries:
        lines.append(f"<code>{e.created_at:%d %b %H:%M}</code> {e.actor_telegram_id} — {e.action}" + (f" ({e.target_type}#{e.target_id})" if e.target_type else ""))

    await query.message.edit_text("\n".join(lines))
    await query.answer()
