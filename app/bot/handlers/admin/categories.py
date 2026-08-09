from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCategoryCB
from app.bot.filters.is_admin import IsAdmin
from app.bot.keyboards.styles import NEUTRAL, PRIMARY, btn
from app.bot.states.category_form import CategoryForm
from app.database.models.user import User
from app.database.repositories.category_repo import CategoryRepo
from app.services.catalog_service import create_category

router = Router(name="admin.categories")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


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
    rows.append([btn("➕ Add Category", AdminCategoryCB(action="add").pack(), PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detail_keyboard(category) -> InlineKeyboardMarkup:
    toggle_label = "🔴 Disable" if category.is_active else "🟢 Enable"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn(
                    toggle_label,
                    AdminCategoryCB(action="toggle", id=str(category.id)).pack(),
                    PRIMARY,
                )
            ],
            [btn("🗑️ Delete", AdminCategoryCB(action="delete", id=str(category.id)).pack(), PRIMARY)],
            [btn("🔙 Back", AdminCategoryCB(action="list").pack(), PRIMARY)],
        ]
    )


@router.callback_query(AdminCategoryCB.filter(F.action == "list"))
async def list_categories(query: CallbackQuery, session: AsyncSession) -> None:
    categories = await CategoryRepo(session).list_all()
    text = "📁 <b>CATEGORY MANAGEMENT</b>\n\n" + (
        "No categories yet." if not categories else f"{len(categories)} categories."
    )
    await query.message.edit_text(text, reply_markup=_list_keyboard(categories))
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


@router.callback_query(AdminCategoryCB.filter(F.action == "add"))
async def start_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CategoryForm.name)
    await query.message.edit_text("➕ <b>Add Category</b>\n\nSend the category name (or /cancel):")
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
    await state.set_state(CategoryForm.emoji)
    await message.answer("Send an emoji for this category (or 'skip'):")


@router.message(CategoryForm.emoji)
async def set_emoji(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    emoji = None if text.lower() == "skip" else text[:16]
    await state.update_data(emoji=emoji)
    await state.set_state(CategoryForm.description)
    await message.answer("Send a short description (or 'skip'):")


@router.message(CategoryForm.description)
async def set_description(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    text = (message.text or "").strip()
    description = None if text.lower() == "skip" else text[:1024]
    data = await state.get_data()

    category_id = await create_category(
        session, name=data["name"], emoji=data.get("emoji"), description=description, image_file_id=None
    )
    await session.flush()
    await state.clear()

    await message.answer(f"✅ Category <b>{data['name']}</b> created (id {category_id}).")
