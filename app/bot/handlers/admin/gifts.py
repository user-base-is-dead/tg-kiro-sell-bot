from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminGiftCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.common import nav_row
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.bot.states.gift_form import (
    GiftAddItemsForm,
    GiftCreateForm,
    GiftEditForm,
    GiftItemEditForm,
)
from app.core.config import get_settings
from app.core.security import get_cipher
from app.database.models.gift import GiftItem, GiftItemStatus, GiftKind, GiftStatus
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.gift_repo import GiftRepo
from app.services.gift_service import (
    add_items,
    available_item_count,
    count_items,
    create_gift_code,
    delete_gift_code,
    delete_item,
    list_items,
    update_gift_code,
    update_item_payload,
)
from app.utils.money import format_minor, parse_to_minor
from app.utils.text import PAD

router = Router(name="admin.gifts")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

_CANCEL_HINT = "\n\nSend /cancel to abort."

# The two branches ask different questions, so "step 3 of 6" is only true on one of them. The flow
# is written down once here and the step counter derived from it, rather than hard-coded into each
# prompt where a reordered question silently makes every later number a lie.
_STEP_FLOW = {
    "item": ["items", "per_user_limit", "expires", "description"],
    "credit": ["value", "max_uses", "per_user_limit", "expires", "description"],
}


def _step(data: dict, field: str) -> str:
    """`(step N of M)` for the current branch. Step 1 is always the kind question."""
    flow = _STEP_FLOW[data.get("kind", "credit")]
    return f"<i>(step {flow.index(field) + 2} of {len(flow) + 1})</i>"


async def _grant_label(session: AsyncSession, gift) -> str:
    """One-line summary of what a code hands over, for lists and detail screens."""
    if gift.kind is GiftKind.ITEM:
        left = await available_item_count(session, gift.id)
        return f"🎁 Gift item — {left} left of {gift.max_uses}"
    return f"💰 {format_minor(gift.value_minor or 0, gift.currency)}"


# ---- List / detail ----


async def _render_list(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    gifts = await GiftRepo(session).list_all()
    rows = []
    for g in gifts:
        grant = await _grant_label(session, g)
        rows.append(
            [
                btn(
                    f"{'🟢' if g.status.value == 'ACTIVE' else '⚫'} ****{g.code_last4} — {grant} ({g.used_count}/{g.max_uses})",
                    AdminGiftCB(action="view", id=str(g.id)).pack(),
                    PRIMARY if g.status.value == "ACTIVE" else NEUTRAL,
                )
            ]
        )
    rows.append([btn("➕ Create Gift Code", AdminGiftCB(action="add").pack(), PRIMARY)])
    rows.append(nav_row("en", back_target="admin_panel", home=False))

    text = (
        "🎁 <b>GIFT CODES</b>\n\n"
        f"{len(gifts)} code(s).\n\n"
        "A code grants either wallet credit or its own gift items. Tap one to see its details, "
        "or create a new one below.\n\n"
        "🟢 = redeemable · ⚫ = exhausted or disabled\n"
        "The counter shows uses so far out of the maximum."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AdminGiftCB.filter(F.action == "list"))
async def list_gifts(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    text, markup = await _render_list(session)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


async def _render_detail(session: AsyncSession, gift_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    gift = await GiftRepo(session).get_by_id(gift_id)
    if gift is None:
        return None

    gid = str(gift.id)
    grant = await _grant_label(session, gift)
    expires_line = f"{gift.expires_at:%d %b %Y}" if gift.expires_at else "never"
    description = gift.description or "<i>(none)</i>"
    active = gift.status is GiftStatus.ACTIVE
    text = (
        f"🎁 <b>****{gift.code_last4}</b>\n\n"
        f"<b>Grants:</b> {grant}\n"
        f"<b>Redeemed:</b> {gift.used_count}/{gift.max_uses}\n"
        f"<b>Per-user limit:</b> {gift.per_user_limit}\n"
        f"<b>Expires:</b> {expires_line}\n"
        f"<b>Status:</b> {gift.status.value}\n\n"
        f"<b>Description shown to users:</b>\n{description}\n"
        # The bubble and its button column share one width, and this screen's longest line is short
        # enough that without the pad the buttons squeeze into a narrow strip. Same trick, same
        # reason as every other detail screen.
        f"{PAD}"
    )

    rows = [
        [
            btn("✏️ Description", f"gedit:ds:{gid}", PRIMARY),
            btn("👤 Per-user limit", f"gedit:pu:{gid}", PRIMARY),
        ],
        [btn("⏳ Expiry", f"gedit:ex:{gid}", PRIMARY)],
    ]
    if gift.kind is GiftKind.ITEM:
        # Topping a code up is the difference between running a second giveaway and extending this
        # one — the code people already have keeps working.
        left, total = await count_items(session, gift.id)
        rows.append([btn("🎁 Add items", AdminGiftCB(action="additems", id=gid).pack(), SUCCESS)])
        rows.append([btn(f"📋 Manage items ({left}/{total} left)", f"gitems:{gid}:0", PRIMARY)])
    rows.append(
        [
            btn(
                "🔴 Disable" if active else "🟢 Enable",
                AdminGiftCB(action="toggle", id=gid).pack(),
                DANGER if active else SUCCESS,
            )
        ]
    )
    rows.append([btn("🗑️ Delete", AdminGiftCB(action="delete", id=gid).pack(), DANGER)])
    rows.append([btn("🔙 Back", AdminGiftCB(action="list").pack(), DANGER)])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(AdminGiftCB.filter(F.action == "view"))
async def view_gift(query: CallbackQuery, callback_data: AdminGiftCB, session: AsyncSession) -> None:
    rendered = await _render_detail(session, int(callback_data.id))
    if rendered is None:
        await query.answer("Not found.", show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(AdminGiftCB.filter(F.action == "toggle"))
async def toggle_gift(query: CallbackQuery, callback_data: AdminGiftCB, session: AsyncSession) -> None:
    """Disable stops a code being claimed without destroying who already claimed it."""
    gift = await GiftRepo(session).get_by_id(int(callback_data.id))
    if gift is None:
        await query.answer("Not found.", show_alert=True)
        return

    if gift.status is GiftStatus.ACTIVE:
        gift.status = GiftStatus.DISABLED
    else:
        # Re-enabling an exhausted/expired code would be a lie unless the reason is gone, so those
        # two are recomputed at claim time anyway; ACTIVE is the honest "try again" state.
        gift.status = GiftStatus.ACTIVE
    await session.flush()

    text, markup = await _render_detail(session, gift.id)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer("Saved.")


@router.callback_query(AdminGiftCB.filter(F.action == "delete"))
async def confirm_delete_gift(query: CallbackQuery, callback_data: AdminGiftCB, session: AsyncSession) -> None:
    gift = await GiftRepo(session).get_by_id(int(callback_data.id))
    if gift is None:
        await query.answer("Not found.", show_alert=True)
        return
    await query.message.edit_text(
        f"⚠️ Delete <b>****{gift.code_last4}</b> permanently?\n\n"
        "<i>Anyone holding this code loses it, its unclaimed items are discarded, and the record of "
        "who claimed what goes with it. People who already claimed keep what they were shown.\n\n"
        "If you only want to stop new claims, use 🔴 Disable instead — that keeps the history.</i>\n"
        f"{PAD}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("🗑️ Yes, delete", AdminGiftCB(action="delete_ok", id=callback_data.id).pack(), DANGER)],
                [btn("🔙 No, keep it", AdminGiftCB(action="view", id=callback_data.id).pack(), PRIMARY)],
            ]
        ),
    )
    await query.answer()


@router.callback_query(AdminGiftCB.filter(F.action == "delete_ok"))
async def delete_gift(query: CallbackQuery, callback_data: AdminGiftCB, session: AsyncSession, user) -> None:
    gift_id = int(callback_data.id)
    gift = await GiftRepo(session).get_by_id(gift_id)
    if gift is None:
        await query.answer("Not found.", show_alert=True)
        return

    last4 = gift.code_last4
    await delete_gift_code(session, gift_id)
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="gift.delete",
        target_type="gift_code",
        target_id=str(gift_id),
    )

    await query.answer("Deleted.")
    text, markup = await _render_list(session)
    await query.message.edit_text(f"🗑️ Deleted <b>****{last4}</b>.\n\n{text}", reply_markup=markup)


# ---- Create Gift wizard ----


@router.callback_query(AdminGiftCB.filter(F.action == "add"))
async def start_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GiftCreateForm.kind)
    await query.message.edit_text(
        # No "of N" here: the total depends on the answer to this very question.
        "➕ <b>Create Gift Code</b>  <i>(step 1)</i>\n\n"
        "<b>What should this code give?</b>\n\n"
        "💰 <b>Wallet credit</b> — tops up the claimer's balance.\n"
        "🎁 <b>Gift item</b> — hands over an item you paste in on the next step. "
        "It has its own stock and never touches your product catalog.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("💰 Wallet credit", "giftkind:credit", SUCCESS)],
                [btn("🎁 Gift item", "giftkind:item", PRIMARY)],
                [btn("🔙 Back", AdminGiftCB(action="list").pack(), DANGER)],
            ]
        ),
    )
    await query.answer()


@router.message(Command("cancel"), GiftCreateForm.items)
@router.message(Command("cancel"), GiftCreateForm.value)
@router.message(Command("cancel"), GiftCreateForm.max_uses)
@router.message(Command("cancel"), GiftCreateForm.per_user_limit)
@router.message(Command("cancel"), GiftCreateForm.expires_days)
@router.message(Command("cancel"), GiftCreateForm.description)
async def cancel_add(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    text, markup = await _render_list(session)
    await message.answer(f"Cancelled — nothing was created.\n\n{text}", reply_markup=markup)


# -- kind --


@router.callback_query(F.data.startswith("giftkind:"), GiftCreateForm.kind)
async def pick_kind(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    kind = query.data.removeprefix("giftkind:")

    if kind == "credit":
        await state.update_data(kind="credit")
        await state.set_state(GiftCreateForm.value)
        await query.message.edit_text(
            "💰 <b>Wallet credit</b>  <i>(step 2 of 6)</i>\n\n"
            "How much should the claimer receive?\n"
            f"Send an amount, e.g. <code>10.00</code>{_CANCEL_HINT}"
        )
        await query.answer()
        return

    # The gift carries its own items, typed here. It deliberately cannot point at a catalog
    # product: a giveaway is not a sale, and letting one draw from `stock_items` meant a promo could
    # quietly empty the shelf a paying customer was queueing for. Gift stock is its own pool.
    await state.update_data(kind="item")
    await state.set_state(GiftCreateForm.items)
    await query.message.edit_text(
        "🎁 <b>Gift item</b>  <i>(step 2 of 5)</i>\n\n"
        "Send the items to give away — licence keys, codes, credentials — <b>one per line</b>. "
        "They are encrypted before they touch the database.\n\n"
        "One claim hands over one line, so the number of lines is the number of people who can "
        "claim this code."
        f"{_CANCEL_HINT}"
    )
    await query.answer()


# -- item branch --


@router.message(GiftCreateForm.items)
async def set_items(message: Message, state: FSMContext) -> None:
    lines = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
    if not lines:
        await message.answer("Send at least one item, one per line, or /cancel:")
        return

    # `max_uses` is not asked for on this branch — it *is* the item count. Any other number is a
    # promise the code cannot keep: too high and a claimer burns their redemption on nothing, too
    # low and items are stranded unclaimable.
    await state.update_data(items=lines)
    await state.set_state(GiftCreateForm.per_user_limit)
    await message.answer(
        f"🎁 {len(lines)} item(s) stored — this code can be claimed {len(lines)} time(s).\n\n"
        f"{_step(await state.get_data(), 'per_user_limit')}\n"
        f"How many times may a single user claim it? Usually <code>1</code>.{_CANCEL_HINT}"
    )


# -- product branch --


# -- credit branch --


@router.message(GiftCreateForm.value)
async def set_value(message: Message, state: FSMContext) -> None:
    try:
        value_minor = parse_to_minor((message.text or "").strip())
        if value_minor <= 0:
            raise ValueError
    except ValueError:
        await message.answer("That isn't a valid amount. Send a positive number, e.g. <code>10.00</code>:")
        return
    await state.update_data(value_minor=value_minor)
    await state.set_state(GiftCreateForm.max_uses)
    await message.answer(
        f"{_step(await state.get_data(), 'max_uses')}\n"
        "How many total redemptions are allowed?\n"
        f"e.g. <code>1</code>, <code>50</code>, <code>1000</code>{_CANCEL_HINT}"
    )


# -- shared tail --


@router.message(GiftCreateForm.max_uses)
async def set_max_uses(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Send a positive whole number, e.g. <code>50</code>:")
        return
    await state.update_data(max_uses=int(text))
    await state.set_state(GiftCreateForm.per_user_limit)
    await message.answer(
        f"{_step(await state.get_data(), 'per_user_limit')}\n"
        "How many times may a single user claim it? Usually <code>1</code>."
        f"{_CANCEL_HINT}"
    )


@router.message(GiftCreateForm.per_user_limit)
async def set_per_user_limit(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Send a positive whole number, e.g. <code>1</code>:")
        return
    await state.update_data(per_user_limit=int(text))
    await state.set_state(GiftCreateForm.expires_days)
    await message.answer(
        f"{_step(await state.get_data(), 'expires')}\n"
        "In how many days should it expire? Send <code>0</code> for never."
        f"{_CANCEL_HINT}"
    )


@router.message(GiftCreateForm.expires_days)
async def set_expires_days(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Send a whole number of days, or <code>0</code> for never:")
        return
    # The day count is stored, not the resolved datetime: FSM data is JSON-serialized into Redis and
    # a `datetime` is not JSON-serializable, so putting one here killed the wizard at this exact
    # step. Everything in FSM state must be a JSON primitive; the datetime is built at creation time
    # (`_expires_at`), which also means the countdown starts when the code is created rather than
    # when the admin happened to answer this question.
    await state.update_data(expires_days=int(text))
    await state.set_state(GiftCreateForm.description)
    await message.answer(
        f"{_step(await state.get_data(), 'description')}\n"
        "Finally, write the description users will read on the claim screen — "
        "say what they're getting and why.\n\n"
        "Send <code>-</code> to leave it blank."
        f"{_CANCEL_HINT}"
    )


@router.message(GiftCreateForm.description)
async def set_description(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    # "-" is the documented way to skip; storing it verbatim would show a stray dash to every user.
    await state.update_data(description=None if raw in ("", "-") else raw)

    data = await state.get_data()
    await state.set_state(GiftCreateForm.review)
    await message.answer(await _review_text(data), reply_markup=_review_keyboard())


def _review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✅ Create code", "giftconfirm", SUCCESS)],
            [btn("❌ Discard", AdminGiftCB(action="list").pack(), DANGER)],
        ]
    )


def _expires_at(data: dict) -> datetime | None:
    """Resolve the stored day count into a real expiry. `0` (or unanswered) means never."""
    days = data.get("expires_days") or 0
    return datetime.now(UTC) + timedelta(days=days) if days > 0 else None


def _wizard_grant(data: dict) -> str:
    """What the code being built will hand over. On the ITEM branch the item count *is* the
    redemption count — `create_gift_code` sets `max_uses` from it — so both read from the same list
    instead of a `max_uses` key that branch never collects."""
    if data["kind"] == "item":
        return f"🎁 Gift item — {len(data['items'])} item(s)"
    return f"💰 {format_minor(data['value_minor'], get_settings().default_currency)}"


def _wizard_max_uses(data: dict) -> int:
    return len(data["items"]) if data["kind"] == "item" else data["max_uses"]


async def _review_text(data: dict) -> str:
    """A last look before the code is generated — it is shown exactly once afterwards, so a typo
    caught here saves creating and disabling a throwaway code."""
    grant = _wizard_grant(data)
    expires_at = _expires_at(data)
    expires = f"{expires_at:%d %b %Y}" if expires_at else "never"
    description = data.get("description") or "<i>(none)</i>"
    return (
        "🔍 <b>Review</b>\n\n"
        f"<b>Grants:</b> {grant}\n"
        f"<b>Total redemptions:</b> {_wizard_max_uses(data)}\n"
        f"<b>Per-user limit:</b> {data['per_user_limit']}\n"
        f"<b>Expires:</b> {expires}\n\n"
        f"<b>Description users will see:</b>\n{description}\n\n"
        "Create this code?"
    )


@router.callback_query(F.data == "giftconfirm", GiftCreateForm.review)
async def confirm_create(query: CallbackQuery, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    is_item = data["kind"] == "item"

    plaintext_code = await create_gift_code(
        session,
        currency=get_settings().default_currency,
        # Ignored for an ITEM code — `create_gift_code` sets it to the item count.
        max_uses=data.get("max_uses", 0),
        per_user_limit=data["per_user_limit"],
        expires_at=_expires_at(data),
        admin_id=user.telegram_id,
        value_minor=None if is_item else data["value_minor"],
        item_payloads=data["items"] if is_item else None,
        description=data.get("description"),
    )
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="gift.create",
        target_type="gift_code",
        metadata={
            "kind": data["kind"],
            # The payloads themselves are never logged — only how many there were.
            **(
                {"item_count": len(data["items"])}
                if is_item
                else {"max_uses": data["max_uses"], "value_minor": data["value_minor"]}
            ),
        },
    )
    await state.clear()

    await query.message.edit_text(
        f"✅ <b>Gift code created</b>\n\n"
        f"<code>{plaintext_code}</code>\n\n"
        f"<b>Grants:</b> {_wizard_grant(data)}\n"
        f"<b>Redemptions:</b> {_wizard_max_uses(data)}\n\n"
        "⚠️ Copy it now — only the last 4 characters are stored, so this is the one and only time "
        "the full code is shown.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[btn("🔙 Back to gift codes", AdminGiftCB(action="list").pack(), PRIMARY)]]
        ),
    )
    await query.answer()


# ---- Editing a code that is already in circulation ----
#
# Only the fields that are safe to change once people may already hold the code. What it *grants*
# is deliberately not editable: changing that out from under a holder is a different code, not an
# edit. `create_gift_code` stays the only way to decide the grant.

_EDIT_PROMPTS = {
    "ds": "Send the new description users will read on the claim screen.\nSend <code>-</code> to clear it.",
    "pu": "How many times may a single user claim it? Send a positive whole number, e.g. <code>1</code>.",
    "ex": "In how many days from now should it expire? Send <code>0</code> for never.",
}
_EDIT_LABELS = {"ds": "Description", "pu": "Per-user limit", "ex": "Expiry"}


@router.callback_query(F.data.startswith("gedit:"))
async def prompt_gift_edit(query: CallbackQuery, state: FSMContext) -> None:
    _, field, gift_id = query.data.split(":", 2)
    if field not in _EDIT_PROMPTS:
        await query.answer("Unknown field.", show_alert=True)
        return

    await state.set_state(GiftEditForm.value)
    await state.update_data(gift_id=int(gift_id), field=field)
    await query.message.edit_text(
        f"✏️ <b>{_EDIT_LABELS[field]}</b>\n\n{_EDIT_PROMPTS[field]}{_CANCEL_HINT}\n{PAD}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[btn("🔙 Back", AdminGiftCB(action="view", id=gift_id).pack(), DANGER)]]
        ),
    )
    await query.answer()


@router.message(Command("cancel"), GiftEditForm.value)
@router.message(Command("cancel"), GiftAddItemsForm.payloads)
async def cancel_gift_edit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    gift_id = (await state.get_data()).get("gift_id")
    await state.clear()
    rendered = await _render_detail(session, gift_id) if gift_id else None
    if rendered is None:
        text, markup = await _render_list(session)
    else:
        text, markup = rendered
    await message.answer(f"Cancelled — nothing changed.\n\n{text}", reply_markup=markup)


@router.message(GiftEditForm.value)
async def apply_gift_edit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    field = data["field"]
    raw = (message.text or "").strip()

    kwargs: dict = {}
    if field == "ds":
        kwargs["description"] = None if raw == "-" else raw[:512]
    elif field == "pu":
        if not raw.isdigit() or int(raw) <= 0:
            await message.answer("Send a positive whole number, e.g. <code>1</code>:")
            return
        kwargs["per_user_limit"] = int(raw)
    else:
        if not raw.isdigit():
            await message.answer("Send a whole number of days, or <code>0</code> for never:")
            return
        days = int(raw)
        kwargs["expires_at"] = datetime.now(UTC) + timedelta(days=days) if days > 0 else None

    await update_gift_code(session, data["gift_id"], **kwargs)
    await state.clear()

    text, markup = await _render_detail(session, data["gift_id"])
    await message.answer(f"✅ {_EDIT_LABELS[field]} updated.\n\n{text}", reply_markup=markup)


@router.callback_query(AdminGiftCB.filter(F.action == "additems"))
async def prompt_add_items(query: CallbackQuery, callback_data: AdminGiftCB, state: FSMContext) -> None:
    await state.set_state(GiftAddItemsForm.payloads)
    await state.update_data(gift_id=int(callback_data.id))
    await query.message.edit_text(
        "🎁 <b>Add items</b>\n\n"
        "Send more items for this code — <b>one per line</b>. They are encrypted before they touch "
        "the database.\n\n"
        "The code's redemption count grows with them, so the code people already have keeps working "
        "and simply stretches further."
        f"{_CANCEL_HINT}\n{PAD}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("🔙 Back", AdminGiftCB(action="view", id=callback_data.id).pack(), DANGER)]
            ]
        ),
    )
    await query.answer()


@router.message(GiftAddItemsForm.payloads)
async def receive_added_items(message: Message, state: FSMContext, session: AsyncSession) -> None:
    lines = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
    if not lines:
        await message.answer("Send at least one item, one per line, or /cancel:")
        return

    gift_id = (await state.get_data())["gift_id"]
    try:
        added = await add_items(session, gift_id, lines)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    # The form stays open so a second batch is just another message, the same as adding stock.
    text, markup = await _render_detail(session, gift_id)
    await message.answer(f"✅ Added {added} item(s).\n\n{text}", reply_markup=markup)
    await state.clear()


# ---- Managing the items inside an ITEM code ----
#
# The pool a code hands out is not fixed once it is created: a key can be typo'd, revoked by the
# vendor, or simply the wrong one. Editing or removing an *unclaimed* line changes only what future
# claimers get, so the code already in circulation keeps working. A *delivered* line is frozen — its
# claimer was shown that exact value, and rewriting or deleting the row would leave the bot
# disagreeing with what that person actually holds.

_ITEMS_PER_PAGE = 8


def _preview(plaintext: str, width: int = 28) -> str:
    """Enough of a line to tell two keys apart in a list, without pasting whole credentials onto
    every screen — the full value lives one tap away."""
    flat = " ".join(plaintext.split())
    return flat if len(flat) <= width else f"{flat[:width]}…"


async def _render_items(
    session: AsyncSession, gift_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    gift = await GiftRepo(session).get_by_id(gift_id)
    if gift is None or gift.kind is not GiftKind.ITEM:
        return None

    left, total = await count_items(session, gift_id)
    pages = max(1, (total + _ITEMS_PER_PAGE - 1) // _ITEMS_PER_PAGE)
    # Deleting the last item on the last page would otherwise strand the admin on an empty screen.
    page = max(0, min(page, pages - 1))
    items = await list_items(session, gift_id, limit=_ITEMS_PER_PAGE, offset=page * _ITEMS_PER_PAGE)
    cipher = get_cipher()

    rows = []
    for number, item in enumerate(items, start=page * _ITEMS_PER_PAGE + 1):
        available = item.status is GiftItemStatus.AVAILABLE
        rows.append(
            [
                btn(
                    f"{'🟢' if available else '✅'} {number}. {_preview(cipher.decrypt(item.payload))}",
                    f"gitem:v:{item.id}",
                    PRIMARY if available else NEUTRAL,
                )
            ]
        )

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("⬅️ Prev", f"gitems:{gift_id}:{page - 1}", NEUTRAL))
        if page < pages - 1:
            nav.append(btn("Next ➡️", f"gitems:{gift_id}:{page + 1}", NEUTRAL))
        rows.append(nav)

    rows.append([btn("➕ Add items", AdminGiftCB(action="additems", id=str(gift_id)).pack(), SUCCESS)])
    rows.append([btn("🔙 Back", AdminGiftCB(action="view", id=str(gift_id)).pack(), DANGER)])

    text = (
        f"📋 <b>Items of ****{gift.code_last4}</b>\n\n"
        f"<b>{left}</b> unclaimed of <b>{total}</b> — so {left} more people can claim this code.\n\n"
        "🟢 = still up for grabs · ✅ = already handed to someone\n"
        "Tap a line to read it in full, edit it, or remove it. Removing an unclaimed line lowers the "
        "redemption count with it."
    )
    if pages > 1:
        text += f"\n\nPage {page + 1} of {pages}"
    return text + f"\n{PAD}", InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("gitems:"))
async def show_items(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    _, gift_id, page = query.data.split(":", 2)
    await state.clear()
    rendered = await _render_items(session, int(gift_id), int(page))
    if rendered is None:
        await query.answer("Not found.", show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


async def _render_item(session: AsyncSession, item_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    item = await session.get(GiftItem, item_id)
    if item is None:
        return None

    available = item.status is GiftItemStatus.AVAILABLE
    # Payloads are arbitrary admin-typed text and the screen is HTML — an unescaped "<" in a key
    # would break the whole message, not just the line.
    payload = html.escape(get_cipher().decrypt(item.payload))
    claimed = f"claimed {item.claimed_at:%d %b %Y}" if item.claimed_at else "claimed"
    text = (
        f"🎁 <b>Gift item #{item.id}</b>\n\n"
        f"<code>{payload}</code>\n\n"
        f"<b>Status:</b> {'unclaimed' if available else claimed}\n"
    )
    if not available:
        text += "\n<i>Already handed over, so it is read-only — the claimer holds this exact value.</i>\n"

    rows = []
    if available:
        rows.append([btn("✏️ Edit", f"gitem:e:{item.id}", PRIMARY), btn("🗑️ Remove", f"gitem:d:{item.id}", DANGER)])
    rows.append([btn("🔙 Back", f"gitems:{item.gift_code_id}:0", DANGER)])
    return text + PAD, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("gitem:v:"))
async def view_item(query: CallbackQuery, session: AsyncSession) -> None:
    rendered = await _render_item(session, int(query.data.rsplit(":", 1)[1]))
    if rendered is None:
        await query.answer("Not found.", show_alert=True)
        return
    text, markup = rendered
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(F.data.startswith("gitem:e:"))
async def prompt_edit_item(query: CallbackQuery, state: FSMContext) -> None:
    item_id = int(query.data.rsplit(":", 1)[1])
    await state.set_state(GiftItemEditForm.payload)
    await state.update_data(item_id=item_id)
    await query.message.edit_text(
        "✏️ <b>Replace this item</b>\n\n"
        "Send the new value for this line — it replaces the old one and is encrypted before it "
        "touches the database.\n\n"
        "Only whoever claims it from now on is affected; the code itself is unchanged."
        f"{_CANCEL_HINT}\n{PAD}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("🔙 Back", f"gitem:v:{item_id}", DANGER)]]),
    )
    await query.answer()


@router.message(Command("cancel"), GiftItemEditForm.payload)
async def cancel_item_edit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    item_id = (await state.get_data()).get("item_id")
    await state.clear()
    rendered = (await _render_item(session, item_id)) if item_id else None
    if rendered is None:
        rendered = await _render_list(session)
    text, markup = rendered
    await message.answer(f"Cancelled — nothing changed.\n\n{text}", reply_markup=markup)


@router.message(GiftItemEditForm.payload)
async def apply_item_edit(message: Message, state: FSMContext, session: AsyncSession, user) -> None:
    item_id = (await state.get_data())["item_id"]
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Send the replacement value, or /cancel:")
        return

    try:
        gift_id = await update_item_payload(session, item_id, raw)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="gift.item.edit",
        target_type="gift_item",
        # The payload itself is never logged — only that it changed.
        target_id=str(item_id),
    )
    await state.clear()

    text, markup = await _render_items(session, gift_id, 0)
    await message.answer(f"✅ Item updated.\n\n{text}", reply_markup=markup)


@router.callback_query(F.data.startswith("gitem:d:"))
async def confirm_delete_item(query: CallbackQuery) -> None:
    item_id = int(query.data.rsplit(":", 1)[1])
    await query.message.edit_text(
        "⚠️ Remove this item?\n\n"
        "<i>It is discarded and the code can be claimed one time fewer. Nobody has received it, so "
        "no one loses anything.</i>\n"
        f"{PAD}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("🗑️ Yes, remove", f"gitem:dok:{item_id}", DANGER)],
                [btn("🔙 No, keep it", f"gitem:v:{item_id}", PRIMARY)],
            ]
        ),
    )
    await query.answer()


@router.callback_query(F.data.startswith("gitem:dok:"))
async def remove_item(query: CallbackQuery, session: AsyncSession, user) -> None:
    item_id = int(query.data.rsplit(":", 1)[1])
    try:
        gift_id = await delete_item(session, item_id)
    except ValueError as exc:
        await query.answer(str(exc), show_alert=True)
        return

    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="gift.item.delete",
        target_type="gift_item",
        target_id=str(item_id),
    )
    await query.answer("Removed.")
    text, markup = await _render_items(session, gift_id, 0)
    await query.message.edit_text(f"🗑️ Item removed.\n\n{text}", reply_markup=markup)
