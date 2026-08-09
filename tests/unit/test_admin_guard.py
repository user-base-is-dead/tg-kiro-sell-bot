from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from aiogram.filters.callback_data import CallbackData

from app.bot import callbacks as cb
from app.bot.handlers.admin.guard import (
    _ADMIN_CB_PREFIXES,
    _ADMIN_COMMANDS,
    _ADMIN_STATE_GROUPS,
    _is_admin_callback,
    deny_admin_callback,
    deny_admin_form_step,
    deny_admin_message,
)
from app.locales.i18n import supported_locales, t

# /cancel is shared with non-admin FSM flows, so the guard must never claim it.
_SHARED_COMMANDS = {"cancel"}
_ADMIN_HANDLERS_DIR = Path(__file__).resolve().parents[2] / "app" / "bot" / "handlers" / "admin"


class _FakeUser:
    """Stands in for the DB User row injected by UserMiddleware."""

    def __init__(self, locale: str = "en", telegram_id: int = 777) -> None:
        self.locale = locale
        self.telegram_id = telegram_id


class _FakeTgUser:
    """Stands in for aiogram's from_user, which only carries the raw Telegram id."""

    def __init__(self, id: int = 777) -> None:  # noqa: A002
        self.id = id


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.from_user = _FakeTgUser()
        self.answers: list[str] = []
        self.markups: list[object] = []

    async def answer(self, text: str, reply_markup=None, **kwargs) -> None:  # noqa: ARG002
        self.answers.append(text)
        self.markups.append(reply_markup)


class _FakeState:
    def __init__(self, state: str | None) -> None:
        self._state = state
        self.cleared = False

    async def get_state(self) -> str | None:
        return self._state

    async def clear(self) -> None:
        self.cleared = True
        self._state = None


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = _FakeTgUser()
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


@pytest.mark.parametrize(
    "data",
    [
        cb.AdminMiscCB(action="settings").pack(),
        cb.AdminProductCB(action="delete", id="3").pack(),
        cb.AdminUserCB(action="ban", id="9").pack(),
        cb.AdminPaymentCB(action="approve", id="1").pack(),
        cb.NavCB(target="admin_panel").pack(),
    ],
)
def test_forged_admin_callbacks_are_recognized(data: str) -> None:
    assert _is_admin_callback(data) is True


@pytest.mark.parametrize(
    "data",
    [
        cb.ProductCB(action="view", id="1").pack(),
        cb.CategoryCB(action="open", id="2").pack(),
        cb.OrderCB(action="confirm", product_id="1").pack(),
        cb.SupportCB(action="create").pack(),
        cb.NavCB(target="home").pack(),
        "noop",
        "",
        None,
    ],
)
def test_user_callbacks_are_left_alone(data: str | None) -> None:
    assert _is_admin_callback(data) is False


def test_every_admin_callback_prefix_is_guarded() -> None:
    """A new Admin*CB factory that isn't listed in the guard would silently fall through to the
    nav catch-all ("unknown action") instead of "not authorized"."""
    declared = {
        obj.__prefix__
        for name, obj in vars(cb).items()
        if name.startswith("Admin") and isinstance(obj, type) and issubclass(obj, CallbackData)
    }
    assert declared
    assert declared == set(_ADMIN_CB_PREFIXES)


def test_every_admin_command_is_guarded() -> None:
    """Source-scan the admin handlers so a newly added admin command can't ship ungated."""
    registered: set[str] = set()
    for path in _ADMIN_HANDLERS_DIR.glob("*.py"):
        if path.name == "guard.py":
            continue
        for match in re.finditer(r"Command\(([^)]*)\)", path.read_text(encoding="utf-8")):
            registered.update(re.findall(r'"([a-z_]+)"', match.group(1)))

    assert registered - _SHARED_COMMANDS == set(_ADMIN_COMMANDS)
    assert _SHARED_COMMANDS.isdisjoint(_ADMIN_COMMANDS)


def test_every_admin_fsm_state_group_is_guarded() -> None:
    """An admin-only wizard missing from the guard would let a demoted admin's next form step
    fall through to the support relay and be delivered to staff as a support message.

    Parsed with ast rather than a regex: a parenthesized multi-line import made the old pattern
    capture "(" as a state group name, so the set compared equal for the wrong reason and the
    check silently stopped guarding anything.
    """
    imported: set[str] = set()
    for path in _ADMIN_HANDLERS_DIR.glob("*.py"):
        if path.name == "guard.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "app.bot.states."
            ):
                imported.update(alias.name for alias in node.names)

    assert imported == {group.__name__ for group in _ADMIN_STATE_GROUPS}


@pytest.mark.asyncio
async def test_demoted_admin_mid_wizard_is_denied_and_state_cleared() -> None:
    message = _FakeMessage("Some half-typed product name")
    state = _FakeState("ProductForm:name")
    await deny_admin_form_step(message, state=state, session=None, user=_FakeUser("en"))
    assert message.answers == [t("common.unauthorized", "en")]
    assert state.cleared is True


def test_guard_is_registered_after_every_admin_router(router_order: tuple[str, ...]) -> None:
    """The guard only ever sees updates the admin routers refused. Registering it too early
    would deny admins; too late (after nav) would let the catch-all answer first."""
    names = list(router_order)

    guard = names.index("admin.guard")
    admin = [i for i, name in enumerate(names) if name.startswith("admin.") and name != "admin.guard"]
    assert admin, "no admin routers registered"
    assert guard > max(admin)
    assert guard < names.index("nav")
    assert guard < names.index("support.relay")


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", supported_locales())
async def test_denied_command_replies_in_the_users_locale(locale: str) -> None:
    message = _FakeMessage("/admin")
    await deny_admin_message(message, session=None, user=_FakeUser(locale))
    assert message.answers == [t("common.unauthorized", locale)]


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", supported_locales())
async def test_pressing_the_stale_admin_button_replaces_the_keyboard(locale: str) -> None:
    """A reply keyboard persists on the client until replaced, so a demoted admin keeps seeing the
    🛡️ row. Pressing it must hand back a keyboard that no longer has it."""
    message = _FakeMessage(t("menu.admin_panel", locale))
    await deny_admin_message(message, session=None, user=_FakeUser(locale))

    markup = message.markups[0]
    assert markup is not None
    labels = [b.text for row in markup.keyboard for b in row]
    assert t("menu.admin_panel", locale) not in labels
    assert t("menu.products", locale) in labels


@pytest.mark.asyncio
async def test_denied_command_leaves_the_keyboard_untouched() -> None:
    message = _FakeMessage("/admin")
    await deny_admin_message(message, session=None, user=_FakeUser("en"))
    assert message.markups == [None]


@pytest.mark.asyncio
async def test_denied_callback_shows_an_alert() -> None:
    query = _FakeQuery(cb.AdminMiscCB(action="settings").pack())
    await deny_admin_callback(query, session=None, user=_FakeUser("en"))
    assert query.answers == [(t("common.unauthorized", "en"), True)]


@pytest.mark.asyncio
async def test_missing_user_falls_back_to_english_instead_of_crashing() -> None:
    message = _FakeMessage("/dashboard")
    await deny_admin_message(message, session=None, user=None)
    assert message.answers == [t("common.unauthorized", "en")]
