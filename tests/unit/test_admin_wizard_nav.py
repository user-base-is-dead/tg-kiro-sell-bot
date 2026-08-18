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

from app.bot.states.product_form import ProductForm
from app.core.config import get_settings

BOT_ID = 99
ADMIN_ID = get_settings().admin_ids[0]


@pytest.fixture(autouse=True)
async def _clean_slate(monkeypatch, dispatcher: Dispatcher):
    """These tests are about FSM transitions, not rendering. Abort re-renders the product list, so
    stub the count seam — otherwise the None session the dispatcher is fed would explode inside
    SQLAlchemy and hide the state assertion being made.

    The storage is also wiped either side: the dispatcher is session-scoped and every test here
    shares one storage key, so the previous test's data would otherwise still be there."""
    from app.bot.handlers.admin import products

    async def _zero(_session, *_a, **_kw) -> int:
        return 0

    async def _none(_session, *_a, **_kw) -> list:
        return []

    for name in ("_count_products", "_count_loose", "_count_in_category"):
        monkeypatch.setattr(products, name, _zero)
    for name in ("_list_page", "_list_loose", "_list_categories", "_list_in_category"):
        monkeypatch.setattr(products, name, _none)
    await _context(dispatcher).clear()
    yield
    await _context(dispatcher).clear()


def _context(dispatcher: Dispatcher) -> FSMContext:
    key = StorageKey(bot_id=BOT_ID, chat_id=ADMIN_ID, user_id=ADMIN_ID)
    return FSMContext(storage=dispatcher.storage, key=key)


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


def test_every_wizard_step_keyboard_has_back_and_abort() -> None:
    from app.bot.handlers.admin.products import _PRODUCT_STEPS, _step_keyboard

    for step in _PRODUCT_STEPS:
        targets = [b.callback_data for row in _step_keyboard(step).inline_keyboard for b in row]
        assert f"pback:{step}" in targets, f"{step} has no Back"
        assert "pabort" in targets, f"{step} has no Abort"


async def test_back_returns_to_previous_step_keeping_answers(dispatcher: Dispatcher) -> None:
    """Back must not throw away what was already typed — retyping the name because you wanted to
    change the price is the whole complaint."""
    bot = AsyncMock()
    bot.id = BOT_ID

    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.price)
    await ctx.update_data(category_id=1, name="Kiro Pro", description="desc")

    result = await dispatcher.feed_update(bot, _callback("pback:price", bot), session=None, user=None)

    assert result is not UNHANDLED, "no handler matched pback:price"
    assert await ctx.get_state() == ProductForm.description
    data = await ctx.get_data()
    assert data["name"] == "Kiro Pro", "Back must preserve earlier answers"


async def test_back_on_the_first_step_aborts(dispatcher: Dispatcher) -> None:
    """There is no step before the first one, so Back exits rather than doing nothing."""
    bot = AsyncMock()
    bot.id = BOT_ID

    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.category)
    await ctx.update_data(category_id=1)

    result = await dispatcher.feed_update(bot, _callback("pback:category", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert await ctx.get_state() is None, "first-step Back clears state like Abort"


async def test_abort_clears_state(dispatcher: Dispatcher) -> None:
    bot = AsyncMock()
    bot.id = BOT_ID

    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.warranty_days)
    await ctx.update_data(name="Kiro Pro")

    result = await dispatcher.feed_update(bot, _callback("pabort", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert await ctx.get_state() is None
    assert await ctx.get_data() == {}


def test_back_walks_the_whole_chain_without_a_gap() -> None:
    """Every step except the first must have a reachable predecessor, or Back silently aborts from
    the middle of the wizard."""
    from app.bot.handlers.admin.products import _PRODUCT_STEPS, _STEP_STATES

    assert set(_PRODUCT_STEPS) == set(_STEP_STATES), "a step with no state cannot be reopened"
