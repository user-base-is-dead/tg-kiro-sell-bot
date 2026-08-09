from __future__ import annotations

import datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from app.bot.states.category_form import CategoryForm
from app.core.config import get_settings

BOT_ID = 99
ADMIN_ID = get_settings().admin_ids[0]


def _context(dispatcher: Dispatcher) -> FSMContext:
    return FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=BOT_ID, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


@pytest.fixture(autouse=True)
async def _clean_slate(monkeypatch, dispatcher: Dispatcher):
    """Storage is session-scoped and shared by every test here, so it is wiped either side. Abort
    re-renders the category list, which these tests do not assert on — stubbing it keeps the None
    session from exploding inside SQLAlchemy and hiding the state assertion."""
    from aiogram.types import InlineKeyboardMarkup

    from app.bot.handlers.admin import categories

    async def _fake_render(_session):
        return "📁 CATEGORY MANAGEMENT", InlineKeyboardMarkup(inline_keyboard=[])

    monkeypatch.setattr(categories, "_render_cat_list", _fake_render)
    await _context(dispatcher).clear()
    yield
    await _context(dispatcher).clear()


def _callback(data: str, bot) -> Update:
    tg = TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin")
    message = Message(
        message_id=10,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=tg,
        text="wizard",
    ).as_(bot)
    return Update(
        update_id=1,
        callback_query=CallbackQuery(id="1", from_user=tg, chat_instance="x", message=message, data=data),
    )


def test_every_category_step_has_back_and_abort() -> None:
    """The category wizard was the same dead end as the product one: /cancel or nothing."""
    from app.bot.handlers.admin.categories import _CATEGORY_STEPS, _cat_step_keyboard

    for step in _CATEGORY_STEPS:
        targets = [b.callback_data for row in _cat_step_keyboard(step).inline_keyboard for b in row]
        assert f"cback:{step}" in targets, f"{step} has no Back"
        assert "cabort" in targets, f"{step} has no Abort"


def test_optional_category_steps_offer_a_skip_button() -> None:
    """Emoji and description were 'type the word skip'."""
    from app.bot.handlers.admin.categories import _cat_step_keyboard

    for step in ("emoji", "description"):
        targets = [b.callback_data for row in _cat_step_keyboard(step).inline_keyboard for b in row]
        assert f"cskip:{step}" in targets, f"{step} cannot be skipped by tapping"


async def test_category_abort_clears_state(dispatcher: Dispatcher) -> None:
    bot = AsyncMock()
    bot.id = BOT_ID
    ctx = _context(dispatcher)
    await ctx.set_state(CategoryForm.emoji)
    await ctx.update_data(name="Software")

    result = await dispatcher.feed_update(bot, _callback("cabort", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert await ctx.get_state() is None
    assert await ctx.get_data() == {}


async def test_category_back_keeps_the_name(dispatcher: Dispatcher) -> None:
    bot = AsyncMock()
    bot.id = BOT_ID
    ctx = _context(dispatcher)
    await ctx.set_state(CategoryForm.description)
    await ctx.update_data(name="Software", emoji="💾")

    result = await dispatcher.feed_update(bot, _callback("cback:description", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert await ctx.get_state() == CategoryForm.emoji
    assert (await ctx.get_data())["name"] == "Software"


async def test_skipping_the_emoji_advances_to_description(dispatcher: Dispatcher) -> None:
    bot = AsyncMock()
    bot.id = BOT_ID
    ctx = _context(dispatcher)
    await ctx.set_state(CategoryForm.emoji)
    await ctx.update_data(name="Software")

    result = await dispatcher.feed_update(bot, _callback("cskip:emoji", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert (await ctx.get_data())["emoji"] is None
    assert await ctx.get_state() == CategoryForm.description
