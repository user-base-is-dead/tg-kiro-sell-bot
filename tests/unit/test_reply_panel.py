from __future__ import annotations

from aiogram.types import ReplyKeyboardRemove

from app.bot.panel import panel_markup, reset_installed_panels

import pytest


@pytest.fixture(autouse=True)
def _clean_install_cache():
    reset_installed_panels()
    yield
    reset_installed_panels()


def test_first_call_removes_the_retired_panel() -> None:
    """The old bottom panel was persistent, which means the client keeps it pinned until the bot
    sends a removal. Closing it from the user's side only hides it until the next message."""
    markup = panel_markup(1, "en", is_admin=False)

    assert isinstance(markup, ReplyKeyboardRemove)


def test_repeat_calls_return_none_so_no_extra_markup_is_sent() -> None:
    """The removal rides on a message the caller was sending anyway. Once it has landed there is
    nothing left to remove, so repeat sends would be pointless traffic."""
    assert panel_markup(1, "en", is_admin=False) is not None
    for _ in range(4):
        assert panel_markup(1, "en", is_admin=False) is None


def test_force_resends_the_removal() -> None:
    """`/start` is the recovery route for a client that somehow still shows the old panel."""
    assert panel_markup(1, "en", is_admin=False) is not None
    assert panel_markup(1, "en", is_admin=False, force=True) is not None


def test_locale_and_admin_do_not_re_trigger_a_removal() -> None:
    """Nothing about a removal varies by either, so neither is part of the key — re-sending on a
    locale change or a promotion would be noise."""
    assert panel_markup(1, "en", is_admin=False) is not None
    assert panel_markup(1, "hi", is_admin=False) is None
    assert panel_markup(1, "en", is_admin=True) is None


def test_each_user_gets_their_own_removal() -> None:
    assert panel_markup(1, "en", is_admin=False) is not None
    assert panel_markup(2, "en", is_admin=False) is not None
