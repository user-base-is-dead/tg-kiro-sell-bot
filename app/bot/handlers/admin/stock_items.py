"""📋 Stock items — every credential of one product, one at a time.

Add Stock only ever added. Once a login was in, there was no way to see it again, fix a typo in it,
or take out one that turned out to be dead — the only tool was deleting the whole product. This is
that missing screen: a product's unsold logins, each one viewable, editable and removable, and its
sold ones with the order and buyer each went to.

What can change is decided by the database at the moment of the change (see stock_service): a login
a buyer reserved while the admin was looking at it is refused, never overwritten.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminProductCB, AdminStockCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.bot.states.product_form import StockEditForm
from app.core.security import get_cipher, normalize_stock_text
from app.database.models.catalog import StockItem, StockStatus
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.stock_repo import CHECKOUT, IN_STOCK, SOLD_BUCKET, StockRepo
from app.database.repositories.user_repo import UserRepo
from app.services import stock_service
from app.services.catalog_service import MAX_PAYLOAD_BYTES
from app.services.stock_hold_service import HOLD_MINUTES
from app.services.stock_service import EditOutcome
from app.utils.pagination import Page
from app.utils.text import PAD, as_admin_wrote_it, escape_html
from app.utils.time import as_utc

router = Router(name="admin.stock_items")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

PAGE_SIZE = 10
# Telegram refuses messages over 4096 characters; the item screen shows the content in full only
# while it fits comfortably under that with the header around it.
_SHOW_IN_FULL = 3500


def _held_now(item: StockItem, now: datetime) -> bool:
    return (
        item.status is StockStatus.HELD
        and item.held_until is not None
        and as_utc(item.held_until) > now
    )


def _is_changeable(item: StockItem, now: datetime) -> bool:
    """Unsold and nobody at checkout with it: the only state an admin may edit or remove."""
    return item.status is StockStatus.AVAILABLE or (
        item.status is StockStatus.HELD and not _held_now(item, now)
    )


def _handle(user) -> str:
    if user is None:
        return "a buyer"
    return f"@{escape_html(user.username)}" if user.username else f"id {user.telegram_id}"


def _plain_handle(user) -> str:
    """The same, for a button label — plain text, so nothing HTML-escaped."""
    if user is None:
        return "—"
    return f"@{user.username}" if user.username else f"id {user.telegram_id}"


async def _checkout_holder(session: AsyncSession, repo: StockRepo, item: StockItem):
    """Who has this item in checkout: the buyer holding it, or the buyer whose order is taking it."""
    if item.status is StockStatus.HELD and item.held_by_user_id is not None:
        return await UserRepo(session).get_by_id(item.held_by_user_id)
    _order, buyer = await repo.sale_of(item)
    return buyer


def _list_cb(pid: int, view: str, page: int = 1) -> str:
    return AdminStockCB(action="list", pid=str(pid), view=view, page=page).pack()


def _item_cb(pid: int, item_id: int, view: str, page: int, action: str = "item") -> str:
    return AdminStockCB(action=action, pid=str(pid), id=str(item_id), view=view, page=page).pack()


# ---- The list ----


async def render_list(
    session: AsyncSession, product_id: int, *, view: str = "u", page_num: int = 1, note: str = ""
) -> tuple[str, InlineKeyboardMarkup] | None:
    product = await ProductRepo(session).get_by_id(product_id)
    if product is None:
        return None

    repo = StockRepo(session)
    counts = {
        "u": await repo.count_in(product_id, IN_STOCK),
        "c": await repo.count_in(product_id, CHECKOUT),
        "s": await repo.count_in(product_id, SOLD_BUCKET),
    }
    view = view if view in counts else "u"
    page = Page(page=page_num, page_size=PAGE_SIZE, total_items=counts[view])

    lines = [
        f"📋 <b>Stock items</b> — {escape_html(product.name)}",
        "",
        f"{note}🟢 In stock: <b>{counts['u']}</b> · 🛒 In checkout: <b>{counts['c']}</b> · "
        f"✅ Sold: <b>{counts['s']}</b>",
        "",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    cipher = get_cipher()

    if view == "u":
        if counts["u"]:
            lines.append(
                "On the shelf, ready to sell. Tap a login to see it in full, edit it or remove it. "
                "When a buyer starts paying it moves to 🛒 In checkout; once the payment is through "
                "it's ✅ Sold — to that one buyer only."
            )
        else:
            lines.append("Nothing in stock. Add logins with 📦 Add Stock.")
        for item in await repo.list_in(product_id, IN_STOCK, offset=page.offset, limit=PAGE_SIZE):
            label = f"🟢 #{item.id} · {stock_service.preview(cipher.decrypt(item.payload))}"
            rows.append([btn(label, _item_cb(product_id, item.id, view, page.clamped_page), NEUTRAL)])
    elif view == "c":
        if counts["c"]:
            lines.append(
                "Taken off the shelf by a buyer who is paying right now — not sold yet. If the "
                f"payment arrives it becomes ✅ Sold; if not, it's back in stock within "
                f"{HOLD_MINUTES} minutes."
            )
        else:
            lines.append("Nobody is checking out right now.")
        for item in await repo.list_in(product_id, CHECKOUT, offset=page.offset, limit=PAGE_SIZE):
            holder = await _checkout_holder(session, repo, item)
            until = (
                f"until {as_utc(item.held_until):%H:%M}"
                if item.status is StockStatus.HELD and item.held_until
                else "paying now"
            )
            rows.append(
                [
                    btn(
                        f"🛒 #{item.id} · {_plain_handle(holder)} · {until}",
                        _item_cb(product_id, item.id, view, page.clamped_page),
                        NEUTRAL,
                    )
                ]
            )
    else:
        if counts["s"]:
            lines.append(
                "Every login that has been sold, newest first, with who got it. Sold logins can't "
                "be edited or removed — they are the record of what each buyer received."
            )
        else:
            lines.append("Nothing sold yet.")
        for item, order, buyer in await repo.list_sold(product_id, offset=page.offset, limit=PAGE_SIZE):
            when = f" · {as_utc(order.placed_at):%d %b}" if order is not None and order.placed_at else ""
            rows.append(
                [
                    btn(
                        f"✅ #{item.id} · {_plain_handle(buyer)}{when}",
                        _item_cb(product_id, item.id, view, page.clamped_page),
                        NEUTRAL,
                    )
                ]
            )
    lines.append(PAD)

    if page.total_pages > 1:
        rows.append(
            [
                btn("◀️", _list_cb(product_id, view, page.clamped_page - 1), PRIMARY)
                if page.has_prev
                else btn(" ", "noop", NEUTRAL),
                btn(f"{page.clamped_page}/{page.total_pages}", "noop", NEUTRAL),
                btn("▶️", _list_cb(product_id, view, page.clamped_page + 1), PRIMARY)
                if page.has_next
                else btn(" ", "noop", NEUTRAL),
            ]
        )
    tabs = (("u", "🟢 In stock"), ("c", "🛒 Checkout"), ("s", "✅ Sold"))
    rows.append(
        [
            btn(
                f"{'• ' if view == code else ''}{label} ({counts[code]})",
                _list_cb(product_id, code),
                PRIMARY if view == code else NEUTRAL,
            )
            for code, label in tabs
        ]
    )
    rows.append([btn("📦 Add Stock", AdminProductCB(action="stock", id=str(product_id)).pack(), SUCCESS)])
    rows.append([btn("🔙 Back to product", AdminProductCB(action="view", id=str(product_id)).pack(), DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AdminStockCB.filter(F.action == "list"))
async def show_list(
    query: CallbackQuery, callback_data: AdminStockCB, session: AsyncSession, state: FSMContext
) -> None:
    await state.clear()
    rendered = await render_list(
        session, int(callback_data.pid), view=callback_data.view, page_num=callback_data.page
    )
    if rendered is None:
        await query.answer("Product not found.", show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


# ---- One item ----


async def render_item(
    session: AsyncSession, product_id: int, item_id: int, *, view: str, page: int, note: str = ""
) -> tuple[str, InlineKeyboardMarkup] | None:
    repo = StockRepo(session)
    item = await repo.get(item_id)
    product = await ProductRepo(session).get_by_id(product_id)
    if item is None or product is None or item.product_id != product_id:
        return None

    now = datetime.now(UTC)
    if item.status is StockStatus.DELIVERED:
        order, buyer = await repo.sale_of(item)
        ref = f"order <code>{order.order_number}</code>" if order is not None else "an order"
        when = f" on {as_utc(order.placed_at):%d %b %Y %H:%M} UTC" if order is not None and order.placed_at else ""
        status = f"✅ Sold — {ref} to {_handle(buyer)}{when}"
    elif item.status is StockStatus.RESERVED:
        order, buyer = await repo.sale_of(item)
        ref = f" for order <code>{order.order_number}</code>" if order is not None else ""
        status = f"🛒 In checkout — {_handle(buyer)}'s payment is going through{ref}. Not sold yet."
    elif _held_now(item, now):
        holder = await _checkout_holder(session, repo, item)
        status = (
            f"🛒 In checkout — {_handle(holder)} is paying for it until "
            f"{as_utc(item.held_until):%H:%M} UTC. Not sold yet: if they don't pay, it goes back in "
            "stock, and it can be edited or removed then."
        )
    elif item.status is StockStatus.VOID:
        status = "⚫ Taken off sale"
    else:
        status = "🟢 In stock — on sale"

    payload = get_cipher().decrypt(item.payload)
    content = payload if len(payload) <= _SHOW_IN_FULL else (
        escape_html(normalize_stock_text(payload)[:_SHOW_IN_FULL]) + " …<i>(too long to show in full)</i>"
    )
    origin = []
    if item.added_by_admin_id:
        origin.append(f"added by <code>{item.added_by_admin_id}</code>")
    if item.batch_id:
        origin.append(f"batch <code>{item.batch_id}</code>")

    lines = [
        f"📦 <b>Stock item #{item.id}</b> — {escape_html(product.name)}",
        "",
        f"{note}{status}",
    ]
    if origin:
        lines.append("· ".join(origin))
    lines += ["", "<b>Content</b>", content]

    rows: list[list[InlineKeyboardButton]] = []
    if _is_changeable(item, now):
        rows.append(
            [
                btn("✏️ Edit", _item_cb(product_id, item.id, view, page, "edit"), PRIMARY),
                btn("🗑️ Remove", _item_cb(product_id, item.id, view, page, "del"), DANGER),
            ]
        )
    rows.append([btn("🔙 Back to list", _list_cb(product_id, view, page), DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AdminStockCB.filter(F.action == "item"))
async def show_item(
    query: CallbackQuery, callback_data: AdminStockCB, session: AsyncSession, state: FSMContext
) -> None:
    # Also the 🔙 Back out of the edit prompt, so it drops the form — otherwise the admin's next
    # message, about anything, would be saved as this login's new content.
    await state.clear()
    rendered = await render_item(
        session,
        int(callback_data.pid),
        int(callback_data.id),
        view=callback_data.view,
        page=callback_data.page,
    )
    if rendered is None:
        await query.answer("That stock item is gone — it may have been removed.", show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


# ---- Edit ----


@router.callback_query(AdminStockCB.filter(F.action == "edit"))
async def prompt_edit(
    query: CallbackQuery, callback_data: AdminStockCB, session: AsyncSession, state: FSMContext
) -> None:
    item = await StockRepo(session).get(int(callback_data.id))
    if item is None or item.product_id != int(callback_data.pid) or not _is_changeable(item, datetime.now(UTC)):
        await query.answer("Only an unsold login can be edited — this one isn't any more.", show_alert=True)
        return

    await state.set_state(StockEditForm.payload)
    await state.update_data(
        edit_stock_id=item.id,
        edit_stock_pid=item.product_id,
        edit_stock_view=callback_data.view,
        edit_stock_page=callback_data.page,
    )
    current = get_cipher().decrypt(item.payload)
    shown = current if len(current) <= _SHOW_IN_FULL else escape_html(stock_service.preview(current, 200))
    await query.message.edit_text(
        f"✏️ <b>Edit stock item #{item.id}</b>\n\n"
        "Send the new content. It replaces the old one exactly as you send it — formatting "
        "included — and is encrypted like the rest. A login that is already in stock or already "
        "sold is refused.\n\n"
        f"<b>Now</b>\n{shown}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    btn(
                        "🔙 Back",
                        _item_cb(item.product_id, item.id, callback_data.view, callback_data.page),
                        DANGER,
                    )
                ]
            ]
        ),
    )
    await query.answer()


@router.message(Command("cancel"), StockEditForm.payload)
async def cancel_edit(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled — the stock item is unchanged.")


@router.message(StockEditForm.payload)
async def apply_edit(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    item_id, product_id = int(data["edit_stock_id"]), int(data["edit_stock_pid"])
    view, page = data.get("edit_stock_view", "u"), int(data.get("edit_stock_page", 1))

    outcome, clash = await stock_service.edit_item(
        session, stock_item_id=item_id, product_id=product_id, new_payload=as_admin_wrote_it(message)
    )
    # These leave the prompt open: the admin just has to send it again, differently.
    if outcome is EditOutcome.EMPTY:
        await message.answer("Send the new content as a message, or /cancel:")
        return
    if outcome is EditOutcome.TOO_LONG:
        await message.answer(
            f"❌ That's too long for one stock item (the limit is {MAX_PAYLOAD_BYTES} bytes). "
            "Send a shorter one, or /cancel:"
        )
        return
    if outcome is EditOutcome.DUPLICATE:
        where = f"stock item #{clash.id}" if clash is not None else "another stock item"
        if clash is not None and clash.product_id and clash.product_id != product_id:
            other = await ProductRepo(session).get_by_id(clash.product_id)
            if other is not None:
                where += f" of <b>{escape_html(other.name)}</b>"
        await message.answer(
            f"❌ Not saved — that login is already {where}. One login can only be sold once.\n\n"
            "Send different content, or /cancel:"
        )
        return

    await state.clear()
    if outcome is EditOutcome.UPDATED:
        await AuditRepo(session).log(
            actor_telegram_id=user.telegram_id,
            action="stock.edit",
            target_type="stock_item",
            target_id=str(item_id),
            metadata={"product_id": product_id},
        )
        note = "✅ Saved.\n"
    elif outcome is EditOutcome.UNCHANGED:
        note = "That's exactly what it already said — nothing changed.\n"
    else:  # NOT_FREE
        note = "❌ Not saved — a buyer took it before your change reached it.\n"

    rendered = await render_item(session, product_id, item_id, view=view, page=page, note=note)
    if rendered is None:
        await message.answer(note + "\nThat stock item is gone.")
        return
    text, markup = rendered
    await message.answer(text, reply_markup=markup)


# ---- Remove ----


@router.callback_query(AdminStockCB.filter(F.action == "del"))
async def confirm_remove(query: CallbackQuery, callback_data: AdminStockCB, session: AsyncSession) -> None:
    item = await StockRepo(session).get(int(callback_data.id))
    if item is None or item.product_id != int(callback_data.pid) or not _is_changeable(item, datetime.now(UTC)):
        await query.answer("Only an unsold login can be removed — this one isn't any more.", show_alert=True)
        return
    glimpse = escape_html(stock_service.preview(get_cipher().decrypt(item.payload), 60))
    await query.message.edit_text(
        f"🗑️ <b>Remove stock item #{item.id}?</b>\n\n"
        f"<code>{glimpse}</code>\n\n"
        "It comes off the shelf now and will never be sold. If you change your mind, you can add it "
        "again with 📦 Add Stock.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    btn(
                        "🗑️ Yes, remove",
                        _item_cb(item.product_id, item.id, callback_data.view, callback_data.page, "del_ok"),
                        DANGER,
                    )
                ],
                [
                    btn(
                        "🔙 No, keep it",
                        _item_cb(item.product_id, item.id, callback_data.view, callback_data.page),
                        PRIMARY,
                    )
                ],
            ]
        ),
    )
    await query.answer()


@router.callback_query(AdminStockCB.filter(F.action == "del_ok"))
async def do_remove(query: CallbackQuery, callback_data: AdminStockCB, session: AsyncSession, user) -> None:
    item_id, product_id = int(callback_data.id), int(callback_data.pid)
    removed = await stock_service.remove_item(session, stock_item_id=item_id, product_id=product_id)
    if not removed:
        await query.answer(
            "Not removed — it was sold, or a buyer has it at checkout right now.", show_alert=True
        )
        rendered = await render_item(
            session, product_id, item_id, view=callback_data.view, page=callback_data.page
        )
    else:
        await AuditRepo(session).log(
            actor_telegram_id=user.telegram_id,
            action="stock.remove",
            target_type="stock_item",
            target_id=str(item_id),
            metadata={"product_id": product_id},
        )
        await query.answer("Removed.")
        rendered = await render_list(
            session,
            product_id,
            view=callback_data.view,
            page_num=callback_data.page,
            note=f"🗑️ Removed stock item #{item_id}.\n\n",
        )
    if rendered is not None:
        text, markup = rendered
        await query.message.edit_text(text, reply_markup=markup)
