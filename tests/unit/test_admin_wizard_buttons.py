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
from app.database.models.catalog import FulfillmentMode

BOT_ID = 99
ADMIN_ID = get_settings().admin_ids[0]


@pytest.fixture(autouse=True)
async def _clean_slate(monkeypatch, dispatcher: Dispatcher):
    """The dispatcher — and so its FSM storage — is session-scoped, and every test here uses the
    same storage key, so leftover data from the previous test would otherwise be visible. Also
    stubs the product count: these tests assert on FSM transitions, and the None session they feed
    the dispatcher would explode inside SQLAlchemy when Abort re-renders the list."""
    from app.bot.handlers.admin import products

    async def _count(_session, **_kw) -> int:
        return 0

    monkeypatch.setattr(products, "_count_products", _count)
    await _context(dispatcher).clear()
    yield
    await _context(dispatcher).clear()


def _context(dispatcher: Dispatcher) -> FSMContext:
    return FSMContext(storage=dispatcher.storage, key=StorageKey(bot_id=BOT_ID, chat_id=ADMIN_ID, user_id=ADMIN_ID))


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


async def test_auto_button_sets_the_same_data_typing_auto_would(dispatcher: Dispatcher) -> None:
    """Typing 'auto' was the complaint. The button must write identical FSM data, or the two paths
    silently produce different products."""
    bot = AsyncMock()
    bot.id = BOT_ID
    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.fulfillment_mode)

    result = await dispatcher.feed_update(bot, _callback("pmode:auto", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert (await ctx.get_data())["fulfillment_mode"] == FulfillmentMode.AUTO
    assert await ctx.get_state() == ProductForm.warranty_days


async def test_manual_button_sets_manual(dispatcher: Dispatcher) -> None:
    bot = AsyncMock()
    bot.id = BOT_ID
    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.fulfillment_mode)

    await dispatcher.feed_update(bot, _callback("pmode:manual", bot), session=None, user=None)

    assert (await ctx.get_data())["fulfillment_mode"] == FulfillmentMode.MANUAL


async def test_warranty_preset_button_sets_days(dispatcher: Dispatcher) -> None:
    bot = AsyncMock()
    bot.id = BOT_ID
    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.warranty_days)

    result = await dispatcher.feed_update(bot, _callback("pwar:30", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert (await ctx.get_data())["warranty_days"] == 30


async def test_custom_warranty_stays_on_the_step_for_typing(dispatcher: Dispatcher) -> None:
    """Custom must not invent a number — it waits for the admin to type one."""
    bot = AsyncMock()
    bot.id = BOT_ID
    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.warranty_days)

    result = await dispatcher.feed_update(bot, _callback("pwar:custom", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert await ctx.get_state() == ProductForm.warranty_days
    assert "warranty_days" not in await ctx.get_data()


async def test_no_category_button_stores_a_real_null(dispatcher: Dispatcher) -> None:
    """The old branch fabricated an "Uncategorized" Category row that leaked into the buyer store."""
    bot = AsyncMock()
    bot.id = BOT_ID
    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.category)

    result = await dispatcher.feed_update(bot, _callback("pickcat:none", bot), session=None, user=None)

    assert result is not UNHANDLED
    data = await ctx.get_data()
    assert "category_id" in data, "the step must record the choice, not leave it absent"
    assert data["category_id"] is None


async def test_skip_description_advances_without_text(dispatcher: Dispatcher) -> None:
    bot = AsyncMock()
    bot.id = BOT_ID
    ctx = _context(dispatcher)
    await ctx.set_state(ProductForm.description)

    result = await dispatcher.feed_update(bot, _callback("pskip:description", bot), session=None, user=None)

    assert result is not UNHANDLED
    assert (await ctx.get_data())["description"] is None
    assert await ctx.get_state() == ProductForm.price


def test_typing_auto_still_works() -> None:
    """The typed handlers stay as a fallback — an admin with the old habit is not punished."""
    from app.bot.handlers.admin import products

    assert any(
        getattr(h.callback, "__name__", "") == "set_fulfillment"
        for h in products.router.message.handlers
    ), "the typed auto/manual handler must remain registered"
