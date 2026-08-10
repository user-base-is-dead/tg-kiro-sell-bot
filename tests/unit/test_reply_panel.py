from __future__ import annotations

from app.bot.panel import panel_markup, reset_installed_panels

import pytest


@pytest.fixture(autouse=True)
def _clean_install_cache():
    reset_installed_panels()
    yield
    reset_installed_panels()


def test_first_call_returns_a_keyboard() -> None:
    markup = panel_markup(1, "en", is_admin=False)

    assert markup is not None
    assert markup.keyboard, "the panel must carry keyboard rows"
    assert markup.is_persistent


def test_repeat_calls_return_none_so_no_extra_message_is_sent() -> None:
    """The keyboard rides on a message the caller was sending anyway. Re-attaching it on every
    screen would be harmless on the client but pointless traffic, so it is handed out once."""
    assert panel_markup(1, "en", is_admin=False) is not None
    for _ in range(4):
        assert panel_markup(1, "en", is_admin=False) is None


def test_force_reissues_an_already_installed_panel() -> None:
    """The install cache has no invalidation path, so without a bypass a user whose client dropped
    the keyboard could never get it back short of a bot restart. `/start` is the recovery route."""
    assert panel_markup(1, "en", is_admin=False) is not None
    assert panel_markup(1, "en", is_admin=False, force=True) is not None


def test_a_locale_change_reissues_the_panel() -> None:
    """The buttons send their own localized label as plain text, so stale labels stop matching the
    `MenuButton` filter. The locale is part of the install key precisely to force a re-issue."""
    assert panel_markup(1, "en", is_admin=False) is not None
    assert panel_markup(1, "hi", is_admin=False) is not None


def test_gaining_admin_reissues_the_panel() -> None:
    plain = panel_markup(1, "en", is_admin=False)
    admin = panel_markup(1, "en", is_admin=True)

    assert plain is not None and admin is not None
    assert len(admin.keyboard) > len(plain.keyboard)


def test_each_user_gets_their_own_install() -> None:
    assert panel_markup(1, "en", is_admin=False) is not None
    assert panel_markup(2, "en", is_admin=False) is not None
