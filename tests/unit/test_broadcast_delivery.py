from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.broadcast_service import _deliver


def _broadcast(body: str = "hello", parts_json: str | None = None, buttons_json: str | None = None):
    return SimpleNamespace(body=body, parts_json=parts_json, buttons_json=buttons_json)


_BUY = json.dumps([[{"text": "🛒 Buy Now", "callback_data": "prod:buy:7"}]])


@pytest.mark.asyncio
async def test_parts_are_copied_in_order() -> None:
    """copy_message is what makes "send exactly what I sent" work for every content type — a photo
    stays a photo, with no per-type branching and no re-upload."""
    bot = AsyncMock()
    parts = [
        {"chat_id": 5, "message_id": 100},
        {"chat_id": 5, "message_id": 101},
        {"chat_id": 5, "message_id": 102},
    ]

    await _deliver(bot, chat_id=777, broadcast=_broadcast(), parts=parts)

    bot.send_message.assert_not_called()
    assert bot.copy_message.await_count == 3
    sent = [c.kwargs["message_id"] for c in bot.copy_message.await_args_list]
    assert sent == [100, 101, 102], "parts must arrive in the order they were composed"
    assert {c.kwargs["chat_id"] for c in bot.copy_message.await_args_list} == {777}


@pytest.mark.asyncio
async def test_legacy_broadcast_without_parts_still_sends_text() -> None:
    """Rows created before media support have parts_json NULL — they must keep working rather
    than silently delivering nothing."""
    bot = AsyncMock()

    await _deliver(bot, chat_id=777, broadcast=_broadcast(body="old style"), parts=None)

    bot.copy_message.assert_not_called()
    bot.send_message.assert_awaited_once_with(777, "old style", reply_markup=None)


@pytest.mark.asyncio
async def test_empty_parts_list_falls_back_to_text() -> None:
    bot = AsyncMock()

    await _deliver(bot, chat_id=777, broadcast=_broadcast(body="fallback"), parts=[])

    bot.send_message.assert_awaited_once_with(777, "fallback", reply_markup=None)


@pytest.mark.asyncio
async def test_a_textless_broadcast_still_carries_its_button() -> None:
    """The announcement path has no parts, so its Buy Now button rides on the text message."""
    bot = AsyncMock()

    await _deliver(bot, chat_id=777, broadcast=_broadcast(buttons_json=_BUY), parts=None)

    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "prod:buy:7"


@pytest.mark.asyncio
async def test_a_composed_broadcast_carries_its_button_on_the_last_part() -> None:
    """The bug: buttons were only ever attached to the parts-less path, so a product attached to a
    composed broadcast produced no button at all. It goes on the last part — a Buy Now hanging off
    part 1 of 3 asks people to decide before they have read the offer, and one button beats three
    identical ones."""
    bot = AsyncMock()
    parts = [{"chat_id": 5, "message_id": 100}, {"chat_id": 5, "message_id": 101}]

    await _deliver(bot, chat_id=777, broadcast=_broadcast(buttons_json=_BUY), parts=parts)

    markups = [c.kwargs["reply_markup"] for c in bot.copy_message.await_args_list]
    assert markups[0] is None
    assert markups[-1].inline_keyboard[0][0].text == "🛒 Buy Now"
