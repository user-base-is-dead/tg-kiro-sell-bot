from __future__ import annotations

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
from app.bot.states.gift_form import GiftCreateForm
from app.core.config import get_settings
from app.database.models.gift import GiftKind
from app.database.repositories.audit_repo import AuditRepo
from app.database.repositories.category_repo import CategoryRepo
from app.database.repositories.gift_repo import GiftRepo
from app.database.repositories.product_repo import ProductRepo
from app.services.gift_service import create_gift_code
from app.utils.money import format_minor, parse_to_minor

router = Router(name="admin.gifts")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

_CANCEL_HINT = "\n\nSend /cancel to abort."


async def _grant_label(session: AsyncSession, gift) -> str:
    """One-line summary of what a code hands over, for lists and detail screens."""
    if gift.kind is GiftKind.PRODUCT:
        product = await ProductRepo(session).get_by_id(gift.product_id) if gift.product_id else None
        return f"📦 {product.name}" if product else "📦 (product deleted)"
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
        "A code grants either wallet credit or a product. Tap one to see its details, "
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


@router.callback_query(AdminGiftCB.filter(F.action == "view"))
async def view_gift(query: CallbackQuery, callback_data: AdminGiftCB, session: AsyncSession) -> None:
    gift = await GiftRepo(session).get_by_id(int(callback_data.id))
    if gift is None:
        await query.answer("Not found.", show_alert=True)
        return

    grant = await _grant_label(session, gift)
    expires_line = f"{gift.expires_at:%d %b %Y}" if gift.expires_at else "never"
    description = gift.description or "<i>(none)</i>"
    text = (
        f"🎁 <b>****{gift.code_last4}</b>\n\n"
        f"<b>Grants:</b> {grant}\n"
        f"<b>Redeemed:</b> {gift.used_count}/{gift.max_uses}\n"
        f"<b>Per-user limit:</b> {gift.per_user_limit}\n"
        f"<b>Expires:</b> {expires_line}\n"
        f"<b>Status:</b> {gift.status.value}\n\n"
        f"<b>Description shown to users:</b>\n{description}"
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
    await state.set_state(GiftCreateForm.kind)
    await query.message.edit_text(
        "➕ <b>Create Gift Code</b>  <i>(step 1 of 6)</i>\n\n"
        "<b>What should this code give?</b>\n\n"
        "💰 <b>Wallet credit</b> — tops up the claimer's balance.\n"
        "📦 <b>Product</b> — delivers a product from your catalog for free. "
        "It uses real stock and shows up in their Orders like a purchase.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("💰 Wallet credit", "giftkind:credit", SUCCESS)],
                [btn("📦 Product", "giftkind:product", PRIMARY)],
                [btn("🔙 Back", AdminGiftCB(action="list").pack(), DANGER)],
            ]
        ),
    )
    await query.answer()


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

    await state.update_data(kind="product")
    categories = await CategoryRepo(session).list_active()
    if not categories:
        await query.answer("No categories yet — add a product first.", show_alert=True)
        return

    rows = [[btn(f"{c.emoji or '📦'} {c.name}", f"giftcat:{c.id}", PRIMARY)] for c in categories]
    rows.append([btn("🔙 Back", AdminGiftCB(action="add").pack(), DANGER)])
    await state.set_state(GiftCreateForm.category)
    await query.message.edit_text(
        "📦 <b>Product gift</b>  <i>(step 2 of 6)</i>\n\nWhich category is the product in?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await query.answer()


# -- product branch --


@router.callback_query(F.data.startswith("giftcat:"), GiftCreateForm.category)
async def pick_category(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    category_id = int(query.data.removeprefix("giftcat:"))
    products = await ProductRepo(session).list_by_category(category_id, active_only=True)
    if not products:
        await query.answer("No active products in that category.", show_alert=True)
        return

    repo = ProductRepo(session)
    rows = []
    for p in products:
        stock = await repo.available_stock_count(p.id)
        # Stock is shown at pick time because a code for an empty product is claimable but will
        # fail at the moment a user tries — better to see it now than to find out from them.
        rows.append([btn(f"{p.name} — {stock} in stock", f"giftprod:{p.id}", PRIMARY if stock else NEUTRAL)])
    rows.append([btn("🔙 Back", AdminGiftCB(action="add").pack(), DANGER)])

    await state.set_state(GiftCreateForm.product)
    await query.message.edit_text(
        "📦 <b>Product gift</b>  <i>(step 3 of 6)</i>\n\nPick the product to give away:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await query.answer()


@router.callback_query(F.data.startswith("giftprod:"), GiftCreateForm.product)
async def pick_product(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    product_id = int(query.data.removeprefix("giftprod:"))
    product = await ProductRepo(session).get_by_id(product_id)
    if product is None:
        await query.answer("That product no longer exists.", show_alert=True)
        return

    await state.update_data(product_id=product_id, product_name=product.name)
    await state.set_state(GiftCreateForm.max_uses)
    await query.message.edit_text(
        f"📦 Giving away: <b>{product.name}</b>\n\n"
        "<i>(step 4 of 6)</i>\n"
        "How many total redemptions? Each one delivers a separate unit of stock.\n"
        f"e.g. <code>1</code>, <code>50</code>{_CANCEL_HINT}"
    )
    await query.answer()


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
        "<i>(step 3 of 6)</i>\n"
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
        "<i>(step 4 of 6)</i>\n"
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
        "<i>(step 5 of 6)</i>\n"
        "In how many days should it expire? Send <code>0</code> for never."
        f"{_CANCEL_HINT}"
    )


@router.message(GiftCreateForm.expires_days)
async def set_expires_days(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Send a whole number of days, or <code>0</code> for never:")
        return
    days = int(text)
    await state.update_data(expires_at=datetime.now(UTC) + timedelta(days=days) if days > 0 else None)
    await state.set_state(GiftCreateForm.description)
    await message.answer(
        "<i>(step 6 of 6)</i>\n"
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


async def _review_text(data: dict) -> str:
    """A last look before the code is generated — it is shown exactly once afterwards, so a typo
    caught here saves creating and disabling a throwaway code."""
    if data["kind"] == "product":
        grant = f"📦 {data['product_name']}"
    else:
        grant = f"💰 {format_minor(data['value_minor'], get_settings().default_currency)}"

    expires_at = data.get("expires_at")
    expires = f"{expires_at:%d %b %Y}" if expires_at else "never"
    description = data.get("description") or "<i>(none)</i>"
    return (
        "🔍 <b>Review</b>\n\n"
        f"<b>Grants:</b> {grant}\n"
        f"<b>Total redemptions:</b> {data['max_uses']}\n"
        f"<b>Per-user limit:</b> {data['per_user_limit']}\n"
        f"<b>Expires:</b> {expires}\n\n"
        f"<b>Description users will see:</b>\n{description}\n\n"
        "Create this code?"
    )


@router.callback_query(F.data == "giftconfirm", GiftCreateForm.review)
async def confirm_create(query: CallbackQuery, state: FSMContext, session: AsyncSession, user) -> None:
    data = await state.get_data()
    is_product = data["kind"] == "product"

    plaintext_code = await create_gift_code(
        session,
        currency=get_settings().default_currency,
        max_uses=data["max_uses"],
        per_user_limit=data["per_user_limit"],
        expires_at=data.get("expires_at"),
        admin_id=user.telegram_id,
        value_minor=None if is_product else data["value_minor"],
        product_id=data["product_id"] if is_product else None,
        description=data.get("description"),
    )
    await AuditRepo(session).log(
        actor_telegram_id=user.telegram_id,
        action="gift.create",
        target_type="gift_code",
        metadata={
            "kind": data["kind"],
            "max_uses": data["max_uses"],
            **({"product_id": data["product_id"]} if is_product else {"value_minor": data["value_minor"]}),
        },
    )
    await state.clear()

    grant = f"📦 {data['product_name']}" if is_product else f"💰 {format_minor(data['value_minor'], get_settings().default_currency)}"
    await query.message.edit_text(
        f"✅ <b>Gift code created</b>\n\n"
        f"<code>{plaintext_code}</code>\n\n"
        f"<b>Grants:</b> {grant}\n"
        f"<b>Redemptions:</b> {data['max_uses']}\n\n"
        "⚠️ Copy it now — only the last 4 characters are stored, so this is the one and only time "
        "the full code is shown.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[btn("🔙 Back to gift codes", AdminGiftCB(action="list").pack(), PRIMARY)]]
        ),
    )
    await query.answer()
