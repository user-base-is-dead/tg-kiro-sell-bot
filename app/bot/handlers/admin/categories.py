from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCategoryCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.common import nav_row
from app.bot.keyboards.styles import DANGER, NEUTRAL, PRIMARY, SUCCESS, btn
from app.bot.states.category_form import CategoryForm
from app.database.models.user import User
from app.database.repositories.category_repo import CategoryRepo
from app.services.catalog_service import create_category

router = Router(name="admin.categories")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

# Same shape as the product wizard's step tuple, for the same reason: Back walks one entry left, so
# a new step cannot be added without also being reachable.
_CATEGORY_STEPS: tuple[str, ...] = ("name", "emoji", "description")

_CAT_STEP_STATES = {
    "name": CategoryForm.name,
    "emoji": CategoryForm.emoji,
    "description": CategoryForm.description,
}


def _cat_step_keyboard(
    step: str, *, extra: list[list[InlineKeyboardButton]] | None = None
) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    if step in ("emoji", "description"):
        rows.append([btn("⏭️ Skip", f"cskip:{step}", PRIMARY)])
    rows.append(
        [
            btn("🔙 Back", f"cback:{step}", DANGER),
            btn("❌ Abort", "cabort", DANGER),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _list_keyboard(categories: list) -> InlineKeyboardMarkup:
    rows = []
    # The 🟢/⚫ prefix stays as the fallback for clients older than Bot API 9.4, which ignore `style`.
    for c in categories:
        dot = "🟢" if c.is_active else "⚫"
        rows.append(
            [
                btn(
                    f"{dot} {c.emoji or '📁'} {c.name}",
                    AdminCategoryCB(action="view", id=str(c.id)).pack(),
                    PRIMARY if c.is_active else NEUTRAL,
                )
            ]
        )
    rows.append([btn("➕ Add Category", AdminCategoryCB(action="add").pack(), SUCCESS)])
    rows.append(nav_row("en", back_target="admin_panel", home=False))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detail_keyboard(category) -> InlineKeyboardMarkup:
    toggle_label = "🔴 Disable" if category.is_active else "🟢 Enable"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn(
                    toggle_label,
                    AdminCategoryCB(action="toggle", id=str(category.id)).pack(),
                    DANGER if category.is_active else SUCCESS,
                )
            ],
            [btn("🗑️ Delete", AdminCategoryCB(action="delete", id=str(category.id)).pack(), DANGER)],
            [btn("🔙 Back", AdminCategoryCB(action="list").pack(), DANGER)],
        ]
    )


@router.callback_query(AdminCategoryCB.filter(F.action == "list"))
async def list_categories(query: CallbackQuery, session: AsyncSession) -> None:
    text, markup = await _render_cat_list(session)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(AdminCategoryCB.filter(F.action == "view"))
async def view_category(query: CallbackQuery, callback_data: AdminCategoryCB, session: AsyncSession) -> None:
    category = await CategoryRepo(session).get_by_id(int(callback_data.id))
    if category is None:
        await query.answer("Category not found.", show_alert=True)
        return
    status = "🟢 Active" if category.is_active else "⚫ Disabled"
    text = (
        f"📁 <b>{category.name}</b>\n\n"
        f"Slug: <code>{category.slug}</code>\n"
        f"Emoji: {category.emoji or '—'}\n"
        f"Description: {category.description or '—'}\n"
        f"Status: {status}"
    )
    await query.message.edit_text(text, reply_markup=_detail_keyboard(category))
    await query.answer()


@router.callback_query(AdminCategoryCB.filter(F.action == "toggle"))
async def toggle_category(query: CallbackQuery, callback_data: AdminCategoryCB, session: AsyncSession) -> None:
    category = await CategoryRepo(session).get_by_id(int(callback_data.id))
    if category is None:
        await query.answer("Category not found.", show_alert=True)
        return
    category.is_active = not category.is_active
    await session.flush()
    await view_category(query, callback_data, session)


@router.callback_query(AdminCategoryCB.filter(F.action == "delete"))
async def delete_category(query: CallbackQuery, callback_data: AdminCategoryCB, session: AsyncSession) -> None:
    category = await CategoryRepo(session).get_by_id(int(callback_data.id))
    if category is None:
        await query.answer("Category not found.", show_alert=True)
        return
    try:
        await CategoryRepo(session).delete(category)
        await session.flush()
    except IntegrityError:
        await session.rollback()
        await query.answer("Can't delete — this category still has products. Disable it instead.", show_alert=True)
        return
    await query.answer("Deleted.")
    categories = await CategoryRepo(session).list_all()
    await query.message.edit_text("📁 <b>CATEGORY MANAGEMENT</b>", reply_markup=_list_keyboard(categories))


# ---- Add Category wizard ----


async def _show_cat_step(
    message: Message, state: FSMContext, step: str, *, edit: bool = True
) -> None:
    """One renderer per step, shared by forward progress and by Back."""
    await state.set_state(_CAT_STEP_STATES[step])
    number = _CATEGORY_STEPS.index(step) + 1
    head = f"➕ <b>Add Category</b> — step {number} of {len(_CATEGORY_STEPS)}\n\n"
    send = message.edit_text if edit else message.answer

    if step == "name":
        await send(
            f"{head}Send the category name (1-128 characters). Buyers see this as a folder in "
            "the store.",
            reply_markup=_cat_step_keyboard("name"),
        )
    elif step == "emoji":
        await send(
            f"{head}Send one emoji to show beside the name, or skip it and 📁 is used.",
            reply_markup=_cat_step_keyboard("emoji"),
        )
    else:
        await send(
            f"{head}Send a short description, or skip it.",
            reply_markup=_cat_step_keyboard("description"),
        )


@router.callback_query(AdminCategoryCB.filter(F.action == "add"))
async def start_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show_cat_step(query.message, state, "name")
    await query.answer()


async def _render_cat_list(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    categories = await CategoryRepo(session).list_all()
    active = sum(1 for c in categories if c.is_active)
    text = (
        "📁 <b>CATEGORY MANAGEMENT</b>\n\n"
        f"{len(categories)} category(ies) · {active} visible to buyers\n\n"
        "Categories are the folders buyers browse. A product does not need one — leave its "
        "category empty and it is listed on its own above the folders.\n\n"
        "<b>Buttons:</b>\n"
        "➕ <b>Add Category</b> — name it, pick an emoji, done\n"
        "Tap any category to rename nothing but toggle or delete it.\n\n"
        "🟢 = visible to buyers · ⚫ = hidden"
    )
    return text, _list_keyboard(categories)


@router.callback_query(F.data == "cabort")
async def abort_cat_wizard(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    text, markup = await _render_cat_list(session)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer("Cancelled.")


@router.callback_query(F.data.startswith("cback:"))
async def back_one_cat_step(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """First-step Back exits, exactly as the product wizard does — there is nothing behind it."""
    step = query.data.removeprefix("cback:")
    if step not in _CATEGORY_STEPS or _CATEGORY_STEPS.index(step) == 0:
        await abort_cat_wizard(query, state, session)
        return
    await _show_cat_step(query.message, state, _CATEGORY_STEPS[_CATEGORY_STEPS.index(step) - 1])
    await query.answer()


@router.callback_query(F.data.startswith("cskip:"))
async def skip_cat_step(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    field = query.data.removeprefix("cskip:")
    if field == "emoji":
        await state.update_data(emoji=None)
        await _show_cat_step(query.message, state, "description")
    elif field == "description":
        await _finish_category(query.message, state, session, description=None)
    await query.answer()


@router.message(Command("cancel"), CategoryForm.name)
@router.message(Command("cancel"), CategoryForm.emoji)
@router.message(Command("cancel"), CategoryForm.description)
async def cancel_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


@router.message(CategoryForm.name)
async def set_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 128:
        await message.answer("Please send a valid name (1-128 chars):")
        return
    await state.update_data(name=name)
    await _show_cat_step(message, state, "emoji", edit=False)


@router.message(CategoryForm.emoji)
async def set_emoji(message: Message, state: FSMContext) -> None:
    """The typed 'skip' stays as a fallback beside the Skip button."""
    text = (message.text or "").strip()
    emoji = None if text.lower() == "skip" else text[:16]
    await state.update_data(emoji=emoji)
    await _show_cat_step(message, state, "description", edit=False)


async def _finish_category(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    description: str | None,
    edit: bool = True,
) -> None:
    """The single exit from the category wizard, whether the description was typed or skipped."""
    data = await state.get_data()
    category_id = await create_category(
        session, name=data["name"], emoji=data.get("emoji"), description=description, image_file_id=None
    )
    await session.flush()
    await state.clear()

    text, markup = await _render_cat_list(session)
    body = f"✅ Category <b>{data['name']}</b> created (id {category_id}).\n\n{text}"
    await (message.edit_text if edit else message.answer)(body, reply_markup=markup)


@router.message(CategoryForm.description)
async def set_description(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    text = (message.text or "").strip()
    description = None if text.lower() == "skip" else text[:1024]
    await _finish_category(message, state, session, description=description, edit=False)
