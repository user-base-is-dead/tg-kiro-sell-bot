from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram.types import Chat, Message
from pydantic import PrivateAttr

from app.bot.handlers.support.relay import dm_relay
from app.bot.middlewares.support_exit_notice import SupportExitNoticeMiddleware
from app.database.models.support import SupportTicket, TicketStatus
from app.database.models.user import User


class _FakeMessage(Message):
    """A real aiogram Message — the middleware isinstance-checks the event, so a duck-typed stand-in
    would be skipped and every assertion here would pass vacuously."""

    _answers: list[str] = PrivateAttr(default_factory=list)

    def __init__(self, chat_type: str = "private") -> None:
        super().__init__(message_id=1, date=datetime.now(UTC), chat=Chat(id=1, type=chat_type))

    async def answer(self, text: str, **_: object) -> None:  # type: ignore[override]
        self._answers.append(text)

    @property
    def answers(self) -> list[str]:
        return self._answers


class _FakeState:
    def __init__(self, data: dict | None = None) -> None:
        self._data = dict(data or {})

    async def get_data(self) -> dict:
        return dict(self._data)

    async def update_data(self, **kwargs: object) -> None:
        self._data.update(kwargs)


class _FakeSession:
    """Stands in for AsyncSession only as far as SupportRepo.get_open_for_user needs."""

    def __init__(self, ticket: SupportTicket | None) -> None:
        self._ticket = ticket

    async def execute(self, _statement: object) -> object:
        ticket = self._ticket
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: ticket))


def _ticket() -> SupportTicket:
    return SupportTicket(
        id=1,
        ticket_number="TCK-A4BD2C",
        user_id=1,
        category="General",
        subject="s",
        status=TicketStatus.OPEN,
        opened_at=datetime.now(UTC),
    )


def _user() -> User:
    return User(id=1, telegram_id=777, username="u", locale="en", referral_code="R1")


async def _run(
    *,
    matched,
    ticket: SupportTicket | None,
    state: _FakeState | None = None,
    message: _FakeMessage | None = None,
) -> tuple[_FakeMessage, _FakeState]:
    message = message or _FakeMessage()
    state = state or _FakeState()
    data = {
        "user": _user(),
        "session": _FakeSession(ticket),
        "state": state,
        "handler": SimpleNamespace(callback=matched) if matched else None,
    }

    async def _handler(_event, _data):
        return None

    await SupportExitNoticeMiddleware()(_handler, message, data)
    return message, state


def _other_handler() -> None:  # any handler that is not the relay
    return None


async def test_menu_press_during_a_live_ticket_warns_the_user() -> None:
    message, _ = await _run(matched=_other_handler, ticket=_ticket())
    assert len(message.answers) == 1
    assert "TCK-A4BD2C" in message.answers[0]


async def test_a_relayed_message_is_never_warned_about() -> None:
    message, _ = await _run(matched=dm_relay, ticket=_ticket())
    assert message.answers == []


async def test_the_warning_does_not_repeat_while_the_user_browses() -> None:
    """Six menu screens in a row must produce one notice, not six."""
    state = _FakeState()
    seen = 0
    for _ in range(6):
        message, state = await _run(matched=_other_handler, ticket=_ticket(), state=state)
        seen += len(message.answers)
    assert seen == 1


async def test_the_warning_rearms_after_the_user_reaches_support_again() -> None:
    state = _FakeState()
    first, state = await _run(matched=_other_handler, ticket=_ticket(), state=state)
    assert len(first.answers) == 1

    _, state = await _run(matched=dm_relay, ticket=_ticket(), state=state)

    again, _ = await _run(matched=_other_handler, ticket=_ticket(), state=state)
    assert len(again.answers) == 1


async def test_no_open_ticket_means_no_notice() -> None:
    message, _ = await _run(matched=_other_handler, ticket=None)
    assert message.answers == []


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_staff_side_chats_are_left_alone(chat_type: str) -> None:
    message, _ = await _run(matched=_other_handler, ticket=_ticket(), message=_FakeMessage(chat_type))
    assert message.answers == []


async def test_a_missing_user_is_not_a_crash() -> None:
    """Service messages arrive with no `user` injected — the same shape that already crashed the
    relay handlers once."""
    message = _FakeMessage()

    async def _handler(_event, _data):
        return None

    await SupportExitNoticeMiddleware()(_handler, message, {"session": None, "state": None, "handler": None})
    assert message.answers == []
