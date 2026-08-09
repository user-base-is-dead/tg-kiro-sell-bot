from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminMiscCB, AdminUserCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.common import nav_row
from app.bot.keyboards.styles import DANGER, PRIMARY, SUCCESS, btn
from app.bot.states.user_search_form import UserSearchForm
from app.core.config import get_settings
from app.database.models.user import UserStatus
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.utils.money import format_minor

router = Router(name="admin.users")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.callback_query(AdminMiscCB.filter(F.action == "users"))
async def prompt_search(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserSearchForm.query)
    await query.message.edit_text(
        "👥 <b>USERS</b>\n\n"
        "Look up any account to inspect or moderate it.\n\n"
        "Send a <b>Telegram ID</b> (e.g. <code>123456789</code>) or a <b>@username</b>.\n"
        "You'll see their order count, wallet balance and signup date, with a Ban/Unban control.\n\n"
        "A partial username matches too — you'll get a list to pick from.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[nav_row("en", back_target="admin_panel", home=False)]
        ),
    )
    await query.answer()


@router.message(Command("cancel"), UserSearchForm.query)
async def cancel_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


def _detail_keyboard(target) -> InlineKeyboardMarkup:
    banned = target.status == UserStatus.BANNED
    toggle = ("✅ Unban", "unban") if banned else ("🚫 Ban", "ban")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn(
                    toggle[0],
                    AdminUserCB(action=toggle[1], id=str(target.id)).pack(),
                    SUCCESS if banned else DANGER,
                )
            ],
            nav_row("en", back_target="admin_panel", home=False),
        ]
    )


async def _render_detail(session: AsyncSession, target) -> str:
    wallet = await WalletRepo(session).get_or_create(target.id, currency=get_settings().default_currency)
    orders = await OrderRepo(session).count_for_user(target.id)
    username = f"@{target.username}" if target.username else "—"
    return (
        f"👤 <b>{target.first_name or username}</b>\n\n"
        f"Username: {username}\n"
        f"ID: <code>{target.telegram_id}</code>\n"
        f"Status: {target.status.value}\n\n"
        f"📦 Orders: {orders}\n"
        f"💳 Balance: {format_minor(wallet.balance_minor, wallet.currency)}\n"
        f"📅 First seen: {target.first_seen_at:%d %b %Y}"
    )


@router.message(UserSearchForm.query)
async def do_search(message: Message, state: FSMContext, session: AsyncSession) -> None:
    query_text = (message.text or "").strip()
    await state.clear()

    results = await UserRepo(session).search(query_text)
    back_only = InlineKeyboardMarkup(
        inline_keyboard=[nav_row("en", back_target="admin_panel", home=False)]
    )
    if not results:
        await message.answer("No matching users.", reply_markup=back_only)
        return
    if len(results) == 1:
        target = results[0]
        await message.answer(await _render_detail(session, target), reply_markup=_detail_keyboard(target))
        return

    rows = [
        [btn(f"@{u.username or u.telegram_id}", AdminUserCB(action="view", id=str(u.id)).pack(), PRIMARY)]
        for u in results
    ]
    rows.append(nav_row("en", back_target="admin_panel", home=False))
    await message.answer(f"Found {len(results)} users:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(AdminUserCB.filter(F.action == "view"))
async def view_user(query: CallbackQuery, callback_data: AdminUserCB, session: AsyncSession) -> None:
    target = await UserRepo(session).get_by_id(int(callback_data.id))
    if target is None:
        await query.answer("Not found.", show_alert=True)
        return
    await query.message.edit_text(await _render_detail(session, target), reply_markup=_detail_keyboard(target))
    await query.answer()


@router.callback_query(AdminUserCB.filter(F.action.in_(["ban", "unban"])))
async def toggle_ban(query: CallbackQuery, callback_data: AdminUserCB, session: AsyncSession, user) -> None:
    target = await UserRepo(session).get_by_id(int(callback_data.id))
    if target is None:
        await query.answer("Not found.", show_alert=True)
        return

    target.status = UserStatus.BANNED if callback_data.action == "ban" else UserStatus.ACTIVE
    await session.flush()
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action=f"user.{callback_data.action}",
        target_type="user",
        target_id=str(target.id),
    )

    await query.answer("Done.")
    await query.message.edit_text(await _render_detail(session, target), reply_markup=_detail_keyboard(target))


