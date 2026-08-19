from __future__ import annotations

import asyncio
import contextlib
import json

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminMiscCB, ProductCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.common import back_keyboard
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.bot.states.broadcast_form import BroadcastForm
from app.core.config import get_settings
from app.database.models.catalog import ProductStatus
from app.database.models.user import User
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.category_repo import CategoryRepo
from app.database.repositories.product_repo import ProductRepo
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
_PRODUCT = "product"

_PRODUCTS_PER_PAGE = 8

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
    await state.update_data(**{_PARTS: [], _PREVIEW_IDS: [], _PRODUCT: None})
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

    data = await state.get_data()
    control = await query.message.answer(
        _control_text(len(parts), data.get(_PRODUCT)),
        reply_markup=_control_keyboard(data.get(_PRODUCT)),
    )
    preview_ids.append(control.message_id)
    await state.update_data(**{_PREVIEW_IDS: preview_ids})
    await state.set_state(BroadcastForm.confirm)


# ---- Attaching a product, so the post is buyable from the post ----
#
# An announcement that ends in "open /products to grab it" asks the reader to go and find the thing
# again, and that walk is where interest is lost. Attaching a product puts the shop's own Buy Now
# button under the post. It is deliberately optional — plenty of broadcasts are not about a
# product at all.


def _control_text(part_count: int, product: dict | None) -> str:
    attached = (
        f"\n🛒 <b>Attached:</b> {product['name']}"
        f" — the post carries a <b>{_button_label(product)}</b> button.\n"
        if product
        else ""
    )
    return (
        "━━━━━━━━━━━━━━━━━━\n"
        f"☝️ {part_count} part(s) above.\n"
        f"{attached}"
        "\n<b>Send</b> delivers this to every user.\n"
        "<b>Back</b> discards it and starts over from an empty draft."
    )


def _button_label(product: dict) -> str:
    """A not-yet-released product cannot be bought, so it gets a look-at-it button instead. Sending
    people to a Buy Now that answers "unknown action" is worse than not linking at all."""
    return "👀 View product" if product["coming_soon"] else "🛒 Buy Now"


def _attached_label(product: dict) -> str:
    """The whole name. It was cut at 20 characters, which is inside the part that tells two
    products apart — "Kiro Pro Max" and "Kiro Pro Plus" both came out as "Kiro Pro"-something, on
    the one screen whose job is confirming which product is about to go out to every user.

    The 64-character ceiling is Telegram's own limit on button text; past it the send fails and the
    admin gets no screen at all, so a name that long is trimmed rather than lost.
    """
    label = f"🛒 Product: {product['name']}"
    return label if len(label) <= 64 else f"{label[:63]}…"


def _control_keyboard(product: dict | None) -> InlineKeyboardMarkup:
    rows = [
        [
            btn(
                _attached_label(product) if product else "🛒 Attach product",
                "broadcast_pickprod:0",
                PRIMARY,
            )
        ]
    ]
    if product:
        rows.append([btn("✖️ Remove product", "broadcast_clearprod", NEUTRAL)])
    rows.append([btn("🔙 Back", "broadcast_back", DANGER), btn("🚀 Send", "broadcast_send", SUCCESS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _buttons_json(product: dict | None) -> str | None:
    """The post's button, as the JSON the broadcast rows carry.

    Hand-built rather than through `btn()` because it is stored as text on the broadcast row and
    rebuilt at send time — but it still has to carry `style`, or it goes out as the one colourless
    button in a bot where every button is styled. Bot API 9.4's field name is what `btn()` sets, so
    the two stay in step.
    """
    if not product:
        return None
    action = "view" if product["coming_soon"] else "buy"
    cb = ProductCB(action=action, id=str(product["id"])).pack()
    # Green for money-in, blue for a look — the same meaning these colours carry everywhere else.
    style = PRIMARY if product["coming_soon"] else SUCCESS
    return json.dumps([[{"text": _button_label(product), "callback_data": cb, "style": style}]])


async def _refresh_control(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await query.message.edit_text(
        _control_text(len(data.get(_PARTS, [])), data.get(_PRODUCT)),
        reply_markup=_control_keyboard(data.get(_PRODUCT)),
    )


_LEGEND = (
    "🛍️ = on sale, gets a <b>Buy Now</b> button\n"
    "🔜 = not released yet, gets a <b>View product</b> button instead — there is nothing to buy "
    "yet, and a dead Buy Now is worse than none."
)


def _product_button(product) -> list:
    soon = product.status is ProductStatus.COMING_SOON
    return [
        btn(
            f"{'🔜' if soon else '🛍️'} {product.name}",
            f"broadcast_setprod:{product.id}",
            NEUTRAL if soon else PRIMARY,
        )
    ]


def _page_nav(target: str, page: int, pages: int) -> list:
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Prev", f"{target}:{page - 1}", NEUTRAL))
    if page < pages - 1:
        nav.append(btn("Next ➡️", f"{target}:{page + 1}", NEUTRAL))
    return nav


@router.callback_query(F.data.startswith("broadcast_pickprod:"), BroadcastForm.confirm)
async def pick_product(query: CallbackQuery, session: AsyncSession) -> None:
    """The store's own shape: loose products out in the open, categories as folders below them.

    Admins recognise their catalog by where things sit, so a flat alphabetical list of every
    product made them read names to find one they could have pointed at. This mirrors what a
    shopper sees, minus the "is it buyable today" filter — a COMING_SOON product is exactly the one
    an announcement is most likely to be about.
    """
    _, _, page_raw = query.data.partition(":")
    page = int(page_raw)
    repo = ProductRepo(session)
    loose = await repo.list_uncategorized(limit=200, active_only=True)
    categories = await CategoryRepo(session).list_active()

    pages = max(1, (len(loose) + _PRODUCTS_PER_PAGE - 1) // _PRODUCTS_PER_PAGE)
    page = max(0, min(page, pages - 1))

    rows = [_product_button(p) for p in loose[page * _PRODUCTS_PER_PAGE : (page + 1) * _PRODUCTS_PER_PAGE]]
    if nav := _page_nav("broadcast_pickprod", page, pages):
        rows.append(nav)
    if loose and categories:
        rows.append([btn("─────────────", "noop", NEUTRAL)])

    # Two per row, same as the store — a folder carries no state worth a full-width button.
    row = []
    for category in categories:
        count = await repo.count_by_category(category.id, active_only=True)
        row.append(
            btn(
                f"{category.emoji or '📦'} {category.name} ({count})",
                f"broadcast_pickcat:{category.id}:0",
                PRIMARY,
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("🔙 Back", "broadcast_prodback", DANGER)])

    if not loose and not categories:
        text = "🛒 <b>Attach a product</b>\n\nNo active products to attach yet."
    else:
        text = (
            "🛒 <b>Attach a product</b>\n\n"
            "Pick the product this post is about and its button goes under the message, so people "
            "can act on it without hunting through the store.\n\n"
            f"{_LEGEND}"
        )
        if categories:
            text += "\n\nProducts outside every category are listed first; the rest are in folders."
        if pages > 1:
            text += f"\n\nLoose products — page {page + 1} of {pages}"

    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()


@router.callback_query(F.data.startswith("broadcast_pickcat:"), BroadcastForm.confirm)
async def pick_product_in_category(query: CallbackQuery, session: AsyncSession) -> None:
    _, category_id, page_raw = query.data.split(":", 2)
    category = await CategoryRepo(session).get_by_id(int(category_id))
    if category is None:
        await query.answer("That category is gone.", show_alert=True)
        return

    repo = ProductRepo(session)
    page = int(page_raw)
    total = await repo.count_by_category(category.id, active_only=True)
    pages = max(1, (total + _PRODUCTS_PER_PAGE - 1) // _PRODUCTS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    products = await repo.list_by_category(
        category.id, active_only=True, offset=page * _PRODUCTS_PER_PAGE, limit=_PRODUCTS_PER_PAGE
    )

    rows = [_product_button(p) for p in products]
    if nav := _page_nav(f"broadcast_pickcat:{category.id}", page, pages):
        rows.append(nav)
    rows.append([btn("🔙 Back to all products", "broadcast_pickprod:0", DANGER)])

    text = (
        f"{category.emoji or '📦'} <b>{category.name}</b>\n\n"
        + (f"{total} product(s).\n\n{_LEGEND}" if total else "Nothing active in this category yet.")
    )
    if pages > 1:
        text += f"\n\nPage {page + 1} of {pages}"

    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()


@router.callback_query(F.data.startswith("broadcast_setprod:"), BroadcastForm.confirm)
async def set_product(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    product = await ProductRepo(session).get_by_id(int(query.data.split(":", 1)[1]))
    if product is None:
        await query.answer("That product is gone.", show_alert=True)
        return
    await state.update_data(
        **{
            _PRODUCT: {
                "id": product.id,
                "name": product.name,
                "coming_soon": product.status is ProductStatus.COMING_SOON,
            }
        }
    )
    await _refresh_control(query, state)
    await query.answer("Attached.")


@router.callback_query(F.data == "broadcast_clearprod", BroadcastForm.confirm)
async def clear_product(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(**{_PRODUCT: None})
    await _refresh_control(query, state)
    await query.answer("Removed.")


@router.callback_query(F.data == "broadcast_prodback", BroadcastForm.confirm)
async def product_pick_back(query: CallbackQuery, state: FSMContext) -> None:
    await _refresh_control(query, state)
    await query.answer()


@router.callback_query(F.data == "broadcast_back", BroadcastForm.confirm)
async def back_to_writing(query: CallbackQuery, state: FSMContext) -> None:
    """Back throws the whole draft away and reopens an empty composer — "wapas start se". The
    preview copies are removed too, so the chat doesn't accumulate the discarded attempt."""
    await query.answer()
    data = await state.get_data()

    await state.set_state(BroadcastForm.body)
    await state.update_data(**{_PARTS: [], _PREVIEW_IDS: [], _PRODUCT: None})

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
        buttons_json=_buttons_json(data.get(_PRODUCT)),
    )
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="broadcast.send",
        target_type="broadcast",
        target_id=str(broadcast.id),
        metadata={
            "total_targets": broadcast.total_targets,
            "parts": len(parts),
            "product_id": (data.get(_PRODUCT) or {}).get("id"),
        },
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
