"""The home screen is one screen, so it must read the same wherever you arrive at it.

The bug: the community-group block was composed inline in `/start` alone. Every other route to the
same screen — Home/Back, a language change, cancelling a form — rendered a bare `welcome.subtitle`,
so the group invite silently vanished the moment the user navigated anywhere and came back.
"""

from __future__ import annotations

import pytest

from app.bot.handlers.gifts import redeem
from app.bot.handlers.payments import topup
from app.bot.handlers.support import create as support_create
from app.bot.handlers.user import language
from app.bot.texts import home_body
from app.core import config

GROUP_URL = "https://t.me/kiro_seller_group"


@pytest.fixture
def group_configured(monkeypatch) -> str:
    monkeypatch.setattr(config.get_settings(), "community_group_url", GROUP_URL, raising=False)
    return GROUP_URL


@pytest.fixture
def no_group(monkeypatch) -> None:
    monkeypatch.setattr(config.get_settings(), "community_group_url", "  ", raising=False)


class _FakeMessage:
    """Stands in for both a sent message and an edited one — the home screen is rendered both ways
    and the text must not depend on which."""

    def __init__(self) -> None:
        self.text: str | None = None
        self.chat = type("C", (), {"id": 1, "type": "private"})()

    async def answer(self, text: str, **_kw: object):
        self.text = text
        return self

    async def edit_text(self, text: str, **_kw: object):
        self.text = text
        return self


class _FakeQuery:
    def __init__(self) -> None:
        self.message = _FakeMessage()

    async def answer(self, text: str = "", **_kw: object):
        return None


class _FakeState:
    def __init__(self) -> None:
        self.cleared = False

    async def clear(self) -> None:
        self.cleared = True


class _User:
    id = 1
    telegram_id = 555
    locale = "en"
    first_name = "Erin"
    username = "erin"


@pytest.fixture
def not_admin(monkeypatch) -> None:
    """`is_admin_user` hits the database; the home text does not depend on the answer."""
    for module in (topup, redeem, support_create, language):
        monkeypatch.setattr(module, "is_admin_user", _false, raising=True)


async def _false(*_a: object, **_kw: object) -> bool:
    return False


def test_home_body_carries_the_group_block(group_configured) -> None:
    body = home_body("en", "Erin")
    assert "Hey Erin!" in body
    assert group_configured in body
    assert "Join our official group" in body


def test_home_body_without_a_configured_group_is_just_the_welcome(no_group) -> None:
    """A deployment with no group must never advertise a dead one."""
    body = home_body("en", "Erin")
    assert "Hey Erin!" in body
    assert "Join our official group" not in body


def test_start_and_home_render_the_same_body(group_configured) -> None:
    """`/start` has no monopoly on the full text."""
    assert home_body("en", "Erin") == home_body("en", "Erin")
    assert GROUP_URL in home_body("en", "Erin")


@pytest.mark.asyncio
async def test_pressing_home_keeps_the_group_block(group_configured, monkeypatch) -> None:
    from app.bot import handlers as _  # noqa: F401
    from app.bot.callbacks import NavCB
    from app.bot.handlers import nav

    monkeypatch.setattr(nav, "is_admin_user", _false, raising=True)
    query = _FakeQuery()
    state = _FakeState()

    await nav.on_nav(query, NavCB(target="home"), state, None, _User())

    assert GROUP_URL in query.message.text
    assert state.cleared, "Home still has to drop a half-finished form"


@pytest.mark.asyncio
async def test_a_language_change_keeps_the_group_block(group_configured, monkeypatch) -> None:
    monkeypatch.setattr(language, "is_admin_user", _false, raising=True)
    monkeypatch.setattr(language, "panel_markup", lambda *a, **k: None, raising=True)

    query = _FakeQuery()

    class _Session:
        async def flush(self) -> None:
            return None

    from app.bot.callbacks import LangCB

    await language.set_language(query, LangCB(locale="en"), _Session(), _User())
    assert GROUP_URL in query.message.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,handler",
    [
        (topup, "cancel_topup"),
        (redeem, "cancel_gift"),
        (support_create, "cancel_create"),
    ],
)
async def test_cancelling_a_form_lands_on_the_full_home_screen(
    group_configured, not_admin, module, handler
) -> None:
    message = _FakeMessage()
    await getattr(module, handler)(message, _FakeState(), None, _User())
    assert GROUP_URL in message.text
