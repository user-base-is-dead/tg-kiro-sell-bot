"""A ban takes away buying, not the ability to explain yourself or to see what the shop sells.

Two rules live in `BanCheckMiddleware` rather than in each handler, because "we forgot to check" is
exactly how both get broken:

  * admins are immune — locking an admin out removes the only place a ban can be undone;
  * support always works — a ban that also silences the appeal channel is not moderation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.types import Update

from app.bot.middlewares.ban_check import BanCheckMiddleware
from app.database.models.user import UserStatus


class _Message:
    def __init__(self, text: str | None) -> None:
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs):  # noqa: ANN003 - test double
        self.answers.append(text)
        return self


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.alerts: list[str] = []

    async def answer(self, text: str = "", **kwargs):  # noqa: ANN003 - test double
        if text:
            self.alerts.append(text)


def _Update(*, message=None, callback_query=None):
    """A real `Update`, because the middleware type-checks it — built with `model_construct` so the
    stand-in message/callback above don't have to satisfy Telegram's full schema."""
    return Update.model_construct(update_id=1, message=message, callback_query=callback_query)


class _State:
    def __init__(self, name: str | None = None) -> None:
        self._name = name

    async def get_state(self) -> str | None:
        return self._name


def _banned(telegram_id: int = 111):
    return SimpleNamespace(status=UserStatus.BANNED, locale="en", telegram_id=telegram_id)


async def _run(event, user, *, state: _State | None = None, admin_ids: set[int] | None = None):
    """Returns True if the update reached the handler."""
    reached = {"value": False}

    async def handler(event, data):  # noqa: ANN001, ARG001 - test double
        reached["value"] = True

    data = {"user": user, "session": None, "state": state or _State()}
    await BanCheckMiddleware()(handler, event, data)
    return reached["value"]


@pytest.mark.asyncio
async def test_an_active_user_is_untouched() -> None:
    active = SimpleNamespace(status=UserStatus.ACTIVE, locale="en", telegram_id=222)

    assert await _run(_Update(message=_Message("anything")), active)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/start", "/products", "/support", "/mytickets", "/cancel"])
async def test_a_banned_user_keeps_the_allowed_commands(command: str) -> None:
    assert await _run(_Update(message=_Message(command)), _banned())


@pytest.mark.asyncio
async def test_a_deep_linked_start_still_counts_as_start() -> None:
    """`/start ref_ABC` and `/start@TheBot` are both /start."""
    assert await _run(_Update(message=_Message("/start ref_ABC123")), _banned())
    assert await _run(_Update(message=_Message("/start@KiroBot")), _banned())


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/topup", "/orders", "/profile", "/gift", "/refer", "/warranty"])
async def test_a_banned_user_loses_every_other_command(command: str) -> None:
    message = _Message(command)

    assert not await _run(_Update(message=message), _banned())
    assert message.answers, "they are told why, not ignored"


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["🚀 Start", "🛍️ Products", "💬 Live Chat"])
async def test_the_allowed_panel_buttons_still_work(label: str) -> None:
    assert await _run(_Update(message=_Message(label)), _banned())


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["💳 Top Up", "🎁 Free Gift", "📦 Orders", "👤 Profile"])
async def test_the_other_panel_buttons_are_refused(label: str) -> None:
    assert not await _run(_Update(message=_Message(label)), _banned())


@pytest.mark.asyncio
async def test_plain_text_reaches_support_when_no_form_is_open() -> None:
    """The DM relay is a fall-through: a reply on an open ticket is just a message."""
    assert await _run(_Update(message=_Message("my order never arrived")), _banned())


@pytest.mark.asyncio
async def test_plain_text_is_blocked_while_a_non_support_form_is_open() -> None:
    """A form left open from before the ban must not become a way back into the action."""
    reached = await _run(
        _Update(message=_Message("50")), _banned(), state=_State("TopUpForm:amount")
    )

    assert not reached


@pytest.mark.asyncio
async def test_plain_text_reaches_the_ticket_form() -> None:
    assert await _run(
        _Update(message=_Message("cannot log in")), _banned(), state=_State("TicketForm:subject")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data",
    [
        "sup:create::",
        "sup:mytickets::",
        "cat:open:3:1",
        "prod:view:7::1",
        "nav:home",
        "nav:categories",
        "nav:cat-3",
        "noop",
    ],
)
async def test_browsing_and_support_callbacks_survive_a_ban(callback_data: str) -> None:
    assert await _run(_Update(callback_query=_Callback(callback_data)), _banned())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data",
    [
        "prod:buy:7::1",
        "ord:confirm:7",
        "ord:wallet:7",
        "aprod:list::1",
        "nav:profile",
        "nav:orders",
        "nav:claim_gift",
    ],
)
async def test_everything_transactional_is_refused(callback_data: str) -> None:
    query = _Callback(callback_data)

    assert not await _run(_Update(callback_query=query), _banned())
    assert query.alerts, "the refusal is explained in an alert"


@pytest.mark.asyncio
async def test_buy_is_refused_even_though_view_is_allowed() -> None:
    """Both ride the `prod:` prefix, so a prefix check alone would hand a banned user checkout."""
    assert await _run(_Update(callback_query=_Callback("prod:view:7::1")), _banned())
    assert not await _run(_Update(callback_query=_Callback("prod:buy:7::1")), _banned())


@pytest.mark.asyncio
async def test_an_admin_carrying_the_banned_flag_is_still_let_through(monkeypatch) -> None:
    """Immunity is enforced here too, not only at the point of banning — a flag set before admins
    were immune must not keep an admin locked out of the panel that could clear it."""
    import app.bot.middlewares.ban_check as module

    async def _yes(session, telegram_id):  # noqa: ANN001, ARG001 - stub
        return True

    monkeypatch.setattr(module, "is_admin_user", _yes)

    assert await _run(_Update(callback_query=_Callback("aprod:list::1")), _banned())
    assert await _run(_Update(message=_Message("/topup")), _banned())
