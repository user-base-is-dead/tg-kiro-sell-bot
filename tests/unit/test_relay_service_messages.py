from __future__ import annotations

import inspect
from datetime import UTC, datetime

from aiogram.types import Chat, Message

from app.bot.handlers.support.relay import dm_relay, group_relay


def _service_message(chat_id: int, chat_type: str) -> Message:
    """What Telegram posts into the group when a forum topic is created or renamed: no text, and
    `from_user` is the bot itself — so UserMiddleware never injects `user`."""
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type),
    )


async def test_group_relay_survives_a_topic_service_message() -> None:
    """The regression this exists for: `user` was a required argument, so aiogram raised TypeError
    on every topic-created/renamed event and ErrorMiddleware answered the support group with
    "Something went wrong on our end" — in front of staff."""
    assert await group_relay(_service_message(-1004466572079, "supergroup"), session=None) is None


async def test_dm_relay_survives_a_missing_user() -> None:
    assert await dm_relay(_service_message(12345, "private"), session=None) is None


def test_user_stays_optional_in_both_relays() -> None:
    for handler in (dm_relay, group_relay):
        param = inspect.signature(handler).parameters["user"]
        assert param.default is None, f"{handler.__name__} must tolerate an absent user"
