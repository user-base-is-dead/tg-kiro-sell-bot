from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram.types import Chat, Message

from app.bot.handlers.support.relay import dm_relay, group_relay, router


def _message(chat_id: int, chat_type: str) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type),
        text="hello",
    )


async def _selected_handler(message: Message):
    """Mirror of aiogram's own dispatch rule: the first handler whose filters pass wins, and
    propagation stops there whatever that handler returns."""
    for handler in router.message.handlers:
        result, _ = await handler.check(message)
        if result:
            return handler.callback
    return None


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
async def test_group_message_goes_to_group_relay(chat_type: str) -> None:
    assert await _selected_handler(_message(-1004466572079, chat_type)) is group_relay


async def test_private_message_goes_to_dm_relay() -> None:
    """The regression this exists for: group_relay was registered first with no filters, so it
    matched every DM as well, ran, returned None, and stopped propagation — dm_relay never fired
    and a user's reply to an open ticket was silently dropped instead of reaching support."""
    assert await _selected_handler(_message(12345, "private")) is dm_relay
