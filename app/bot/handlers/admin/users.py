from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminMiscCB, AdminUserCB
from app.bot.filters.is_admin import IsAdmin, is_admin_user
from app.bot.keyboards.common import nav_row
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.bot.states.user_search_form import UserBalanceForm, UserSearchForm
from app.core.config import get_settings
from app.database.models.user import UserStatus
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services import wallet_service
from app.utils.errors import UserError
from app.utils.money import format_minor, parse_to_minor
from app.utils.pagination import Page

router = Router(name="admin.users")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

PAGE_SIZE = 20

def _handle(target) -> str:
    return f"@{target.username}" if target.username else "—"


def _list_keyboard(users: list, page: Page) -> InlineKeyboardMarkup:
    """Numbered buttons rather than one row per username: 20 full-width rows is a wall a phone has
    to scroll past. The number is the member's signup rank and matches the text above, so #1 is
    always the very first person who joined, on whatever page they fall."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for offset, target in enumerate(users):
        rank = page.offset + offset + 1
        banned = target.status == UserStatus.BANNED
        row.append(
            btn(
                f"{'🚫' if banned else ''}{rank}",
                AdminUserCB(action="view", id=str(target.id), page=page.clamped_page).pack(),
                DANGER if banned else PRIMARY,
            )
        )
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav: list[InlineKeyboardButton] = []
    if page.has_prev:
        nav.append(
            btn("◀️ Previous", AdminUserCB(action="list", page=page.clamped_page - 1).pack(), PRIMARY)
        )
    if page.total_pages > 1:
        nav.append(btn(f"{page.clamped_page}/{page.total_pages}", "noop", NEUTRAL))
    if page.has_next:
        nav.append(
            btn("Next ▶️", AdminUserCB(action="list", page=page.clamped_page + 1).pack(), PRIMARY)
        )
    if nav:
        rows.append(nav)

    rows.append([btn("🔍 Search by ID or @username", AdminUserCB(action="search").pack(), PRIMARY)])
    rows.append(nav_row("en", back_target="admin_panel", home=False))
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _count_users(session: AsyncSession) -> int:
    return await UserRepo(session).count_all()


async def _users_page(session: AsyncSession, *, offset: int, limit: int) -> list:
    return await UserRepo(session).list_page(offset=offset, limit=limit)

async def _render_list(session: AsyncSession, page_num: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await _count_users(session)
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=total)
    users = await _users_page(session, offset=page.offset, limit=PAGE_SIZE) if total else []

    if not total:
        return (
            "👥 <b>USERS</b>\n\nNobody has messaged the bot yet. Accounts appear here the moment "
            "someone sends /start.",
            _list_keyboard([], page),
        )

    lines = [
        "👥 <b>USERS</b>",
        "",
        f"<b>{total}</b> member(s) joined · oldest first, newest at the bottom",
        f"Showing {page.offset + 1}–{page.offset + len(users)}",
        "",
    ]
    for offset, target in enumerate(users):
        rank = page.offset + offset + 1
        flag = "🚫 " if target.status == UserStatus.BANNED else ""
        # The Telegram ID is <code> so it can be tapped to copy — it is what /adjust_balance and
        # every other lookup takes.
        lines.append(
            f"<b>{rank}.</b> {flag}{_handle(target)} · <code>{target.telegram_id}</code>\n"
            f"     {target.first_seen_at:%d %b %Y, %H:%M}"
        )

    lines += [
        "",
        "Tap a number below to open that member's profile — full history, wallet balance, "
        "and controls to credit or ban them.",
        "🚫 = banned",
    ]
    return "\n".join(lines), _list_keyboard(users, page)


@router.callback_query(AdminMiscCB.filter(F.action == "users"))
async def open_users(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """The panel button used to open a search prompt with no way to see who had actually joined."""
    await state.clear()
    text, markup = await _render_list(session, 1)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(AdminUserCB.filter(F.action == "list"))
async def list_users(query: CallbackQuery, callback_data: AdminUserCB, session: AsyncSession) -> None:
    text, markup = await _render_list(session, callback_data.page)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()

def _detail_keyboard(target, page: int, *, target_is_admin: bool = False) -> InlineKeyboardMarkup:
    banned = target.status == UserStatus.BANNED
    toggle = ("✅ Unban", "unban") if banned else ("🚫 Ban", "ban")
    # An admin cannot be banned, so the button says so instead of offering an action that will be
    # refused. Unban stays live for them: a stale BANNED flag from before admins were immune should
    # still be clearable.
    ban_row = (
        [btn("🛡️ Admin — cannot be banned", "noop", NEUTRAL)]
        if target_is_admin and not banned
        else [
            btn(
                toggle[0],
                AdminUserCB(action=toggle[1], id=str(target.id), page=page).pack(),
                SUCCESS if banned else DANGER,
            )
        ]
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                # Just "Wallet": this is the only place a balance can move, because users have no
                # self-service withdrawal — they ask support and an admin does it here by hand. The
                # old "Credit / Debit" label read like a payment card rather than the accounting
                # sense of the words.
                btn(
                    "💰 Wallet",
                    AdminUserCB(action="credit", id=str(target.id), page=page).pack(),
                    SUCCESS,
                )
            ],
            ban_row,
            # One Back, not two. This used to carry "🔙 Back to list" *and* a plain "🔙 Back" to the
            # admin panel — two red buttons with the same word on them, where the second skipped
            # past the list the admin had just been navigating. Back to list returns to the page
            # they came from, not to page 1: losing your place after every profile is what makes a
            # 45-page list unusable.
            [btn("🔙 Back to list", AdminUserCB(action="list", page=page).pack(), DANGER)],
        ]
    )


async def _render_detail(session: AsyncSession, target) -> str:
    settings = get_settings()
    wallet = await WalletRepo(session).get_or_create(target.id, currency=settings.default_currency)
    order_repo = OrderRepo(session)
    order_count = await order_repo.count_for_user(target.id)
    recent = await order_repo.list_for_user(target.id, offset=0, limit=5)

    full_name = " ".join(p for p in (target.first_name, target.last_name) if p) or "—"
    referrer = None
    if target.referred_by_id is not None:
        referrer = await UserRepo(session).get_by_id(target.referred_by_id)

    lines = [
        f"👤 <b>{full_name}</b>",
        "",
        f"Username: {_handle(target)}",
        f"Telegram ID: <code>{target.telegram_id}</code>",
        f"Status: {'🚫 BANNED' if target.status == UserStatus.BANNED else '🟢 ACTIVE'}",
        f"Language: {target.locale}",
        "",
        f"📅 Joined: {target.first_seen_at:%d %b %Y, %H:%M}",
        f"👋 Last seen: {target.last_seen_at:%d %b %Y, %H:%M}",
        "",
        f"💳 Wallet: <b>{format_minor(wallet.balance_minor, wallet.currency)}</b>",
        f"📦 Orders: {order_count}",
    ]

    if recent:
        lines.append("")
        lines.append("<b>Recent orders:</b>")
        for order in recent:
            lines.append(
                f"  <code>{order.order_number}</code> · "
                f"{format_minor(order.total_minor, order.currency)} · {order.status.value}"
            )

    lines += [
        "",
        f"🔗 Referral code: <code>{target.referral_code}</code>",
        f"👥 Referred by: {_handle(referrer) if referrer else '—'}",
    ]
    if target.notes:
        lines += ["", f"📝 Notes: {target.notes}"]

    return "\n".join(lines)


@router.callback_query(AdminUserCB.filter(F.action == "view"))
async def view_user(query: CallbackQuery, callback_data: AdminUserCB, session: AsyncSession) -> None:
    target = await UserRepo(session).get_by_id(int(callback_data.id))
    if target is None:
        await query.answer("Not found.", show_alert=True)
        return
    await query.message.edit_text(
        await _render_detail(session, target),
        reply_markup=_detail_keyboard(
            target, callback_data.page, target_is_admin=await is_admin_user(session, target.telegram_id)
        ),
    )
    await query.answer()

@router.callback_query(AdminUserCB.filter(F.action.in_(["ban", "unban"])))
async def toggle_ban(query: CallbackQuery, callback_data: AdminUserCB, session: AsyncSession, user) -> None:
    target = await UserRepo(session).get_by_id(int(callback_data.id))
    if target is None:
        await query.answer("Not found.", show_alert=True)
        return

    # Admins are immune. Banning one is never a legitimate moderation action and always an accident
    # or an attack — one admin could lock out the owner, or lock themselves out of the panel they
    # would need to undo it. `BanCheckMiddleware` ignores the flag for admins anyway, so setting it
    # here would only produce a BANNED-looking profile that behaves as active: worse than refusing.
    if callback_data.action == "ban" and await is_admin_user(session, target.telegram_id):
        await query.answer("Admins cannot be banned.", show_alert=True)
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
    await query.message.edit_text(
        await _render_detail(session, target),
        reply_markup=_detail_keyboard(
            target, callback_data.page, target_is_admin=await is_admin_user(session, target.telegram_id)
        ),
    )


# ---- Credit / debit a wallet from the profile ----


@router.callback_query(AdminUserCB.filter(F.action == "credit"))
async def prompt_credit(query: CallbackQuery, callback_data: AdminUserCB, state: FSMContext, session: AsyncSession) -> None:
    target = await UserRepo(session).get_by_id(int(callback_data.id))
    if target is None:
        await query.answer("Not found.", show_alert=True)
        return

    wallet = await WalletRepo(session).get_or_create(
        target.id, currency=get_settings().default_currency
    )
    await state.set_state(UserBalanceForm.amount)
    await state.update_data(target_id=target.id, page=callback_data.page)
    await query.message.edit_text(
        f"💰 <b>Wallet</b>\n\n"
        f"{_handle(target)} · <code>{target.telegram_id}</code>\n"
        f"Current balance: <b>{format_minor(wallet.balance_minor, wallet.currency)}</b>\n\n"
        "Send the amount to move, signed:\n"
        "<code>+10</code> adds 10 · <code>-2.50</code> takes away 2.50\n\n"
        "Add a reason after it if you want one recorded in the audit log:\n"
        "<code>+10 goodwill for the delayed order</code>\n\n"
        "A debit cannot take the balance below zero.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    btn(
                        "🔙 Back",
                        AdminUserCB(action="view", id=str(target.id), page=callback_data.page).pack(),
                        DANGER,
                    )
                ]
            ]
        ),
    )
    await query.answer()


@router.message(Command("cancel"), UserBalanceForm.amount)
async def cancel_credit(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")

@router.message(UserBalanceForm.amount)
async def apply_credit(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    target = await UserRepo(session).get_by_id(data["target_id"])
    if target is None:
        await state.clear()
        await message.answer("❌ That account no longer exists.")
        return

    parts = (message.text or "").strip().split(maxsplit=1)
    raw_amount = parts[0] if parts else ""
    reason = parts[1] if len(parts) > 1 else "Manual admin adjustment"

    # An unsigned number is ambiguous — "10" could mean credit or debit, and guessing wrong moves
    # real money the wrong way.
    if not raw_amount.startswith(("+", "-")):
        await message.answer(
            "Start the amount with <b>+</b> or <b>-</b> so the direction is explicit, "
            "e.g. <code>+10</code> or <code>-2.50</code>:"
        )
        return
    try:
        amount_minor = parse_to_minor(raw_amount)
    except ValueError:
        await message.answer("That isn't a valid amount. Try <code>+10</code> or <code>-2.50</code>:")
        return
    if amount_minor == 0:
        await message.answer("Amount can't be zero.")
        return

    currency = get_settings().default_currency
    try:
        txn = await wallet_service.admin_adjust(
            session,
            user_id=target.id,
            amount_minor=amount_minor,
            currency=currency,
            reason=reason,
        )
    except UserError:
        await message.answer("❌ That would take their balance below zero. Nothing changed.")
        return

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="wallet.admin_adjust",
        target_type="user",
        target_id=str(target.id),
        metadata={"amount_minor": amount_minor, "reason": reason},
    )
    await session.flush()
    await state.clear()

    verb = "Credited" if amount_minor > 0 else "Debited"
    await message.answer(
        f"✅ {verb} {format_minor(abs(amount_minor), currency)} "
        f"{'to' if amount_minor > 0 else 'from'} {_handle(target)}.\n"
        f"New balance: <b>{format_minor(txn.balance_after_minor, currency)}</b>\n\n"
        + await _render_detail(session, target),
        reply_markup=_detail_keyboard(
            target, data.get("page", 1), target_is_admin=await is_admin_user(session, target.telegram_id)
        ),
    )


# ---- Search, for finding one account in a long list ----


@router.callback_query(AdminUserCB.filter(F.action == "search"))
async def prompt_search(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserSearchForm.query)
    await query.message.edit_text(
        "🔍 <b>Find a user</b>\n\n"
        "Send a <b>Telegram ID</b> (e.g. <code>123456789</code>) or a <b>@username</b>.\n"
        "A partial username matches too — you'll get a list to pick from.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("🔙 Back to list", AdminUserCB(action="list").pack(), DANGER)],
                nav_row("en", back_target="admin_panel", home=False),
            ]
        ),
    )
    await query.answer()


@router.message(Command("cancel"), UserSearchForm.query)
async def cancel_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")

@router.message(UserSearchForm.query)
async def do_search(message: Message, state: FSMContext, session: AsyncSession) -> None:
    query_text = (message.text or "").strip()
    await state.clear()

    results = await UserRepo(session).search(query_text)
    back_to_list = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("🔙 Back to list", AdminUserCB(action="list").pack(), DANGER)],
            nav_row("en", back_target="admin_panel", home=False),
        ]
    )
    if not results:
        await message.answer(
            f"No user matches <b>{query_text}</b>.\n\n"
            "They only appear here once they've sent the bot at least one message.",
            reply_markup=back_to_list,
        )
        return
    if len(results) == 1:
        target = results[0]
        await message.answer(
            await _render_detail(session, target),
            reply_markup=_detail_keyboard(
                target, 1, target_is_admin=await is_admin_user(session, target.telegram_id)
            ),
        )
        return

    rows = [
        [
            btn(
                f"{'🚫 ' if u.status == UserStatus.BANNED else ''}{_handle(u)} · {u.telegram_id}",
                AdminUserCB(action="view", id=str(u.id)).pack(),
                DANGER if u.status == UserStatus.BANNED else PRIMARY,
            )
        ]
        for u in results
    ]
    rows.append([btn("🔙 Back to list", AdminUserCB(action="list").pack(), DANGER)])
    rows.append(nav_row("en", back_target="admin_panel", home=False))
    await message.answer(
        f"Found <b>{len(results)}</b> match(es) for <b>{query_text}</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
