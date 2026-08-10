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

from app.bot.states.broadcast_form import BroadcastForm
from app.core.config import get_settings

BOT_ID = 99
# The broadcast router carries a real IsAdmin filter. Rather than stubbing it out — which would
# stop the test exercising the filter chain the bug lived in — use an id settings already trust.
ADMIN_ID = get_settings().admin_ids[0]


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
        text="preview",
    ).as_(bot)
    query = CallbackQuery(id="1", from_user=tg, chat_instance="x", message=message, data=data)
    return Update(update_id=1, callback_query=query)


@pytest.mark.asyncio
async def test_back_from_preview_resets_to_an_empty_draft(dispatcher: Dispatcher) -> None:
    """Back reported as "not working" is the reason this exists: it drives the update through the
    real dispatcher, so a mismatched state filter or a shadowing router fails the test rather than
    silently doing nothing in the user's chat."""
    bot = AsyncMock()
    bot.id = BOT_ID

    ctx = _context(dispatcher)
    await ctx.set_state(BroadcastForm.confirm)
    await ctx.update_data(
        parts=[{"chat_id": ADMIN_ID, "message_id": 1, "label": "💬 hi"}],
        preview_message_ids=[11, 12],
    )

    result = await dispatcher.feed_update(bot, _callback("broadcast_back", bot), session=None, user=None)

    assert result is not UNHANDLED, "no handler matched broadcast_back"
    assert await ctx.get_state() == BroadcastForm.body
    data = await ctx.get_data()
    assert data["parts"] == [], "Back must discard the draft"
    # The discarded preview is cleaned out of the chat rather than left lying around.
    assert bot.delete_message.await_count == 2


@pytest.mark.asyncio
async def test_done_with_an_empty_draft_is_rejected(dispatcher: Dispatcher) -> None:
    bot = AsyncMock()
    bot.id = BOT_ID

    ctx = _context(dispatcher)
    await ctx.set_state(BroadcastForm.body)
    await ctx.update_data(parts=[], preview_message_ids=[])

    result = await dispatcher.feed_update(bot, _callback("broadcast_done", bot), session=None, user=None)

    assert result is not UNHANDLED
    # Still writing — an empty broadcast never reaches the preview.
    assert await ctx.get_state() == BroadcastForm.body
