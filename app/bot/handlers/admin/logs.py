from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminMiscCB, NavCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.common import with_nav
from app.database.repositories.audit_repo import AuditRepo

router = Router(name="admin.logs")
router.callback_query.filter(IsAdmin())


@router.callback_query(AdminMiscCB.filter(F.action == "logs"))
async def show_logs(query: CallbackQuery, session: AsyncSession) -> None:
    entries = await AuditRepo(session).list_recent(20)
    if not entries:
        text = "📝 <b>Audit Logs</b>\n\nNo audit entries yet. Logs appear here when you perform admin actions like sending broadcasts, managing products, or adjusting user balances."
    else:
        lines = ["📝 <b>Audit Logs</b> (most recent 20 entries)\n"]
        for e in entries:
            lines.append(f"<code>{e.created_at:%d %b %H:%M}</code> {e.actor_telegram_id} — {e.action}" + (f" ({e.target_type}#{e.target_id})" if e.target_type else ""))
        text = "\n".join(lines)

    markup = with_nav([], "en", back_target="admin_panel", home=False)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()
