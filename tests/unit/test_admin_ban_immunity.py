"""An admin cannot be banned — enforced at the point of banning, not only in the middleware.

Setting the flag on an admin would produce a profile that reads BANNED while behaving as active
(`BanCheckMiddleware` lets admins through regardless), which is more confusing than a refusal.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.bot.handlers.admin.users import _detail_keyboard
from app.database.models.user import UserStatus


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_an_ordinary_member_can_be_banned() -> None:
    target = SimpleNamespace(id=5, status=UserStatus.ACTIVE)

    markup = _detail_keyboard(target, 1, target_is_admin=False)

    assert any("Ban" in label for label in _labels(markup))
    assert any(c and c.startswith("auser:ban") for c in _callbacks(markup))


def test_an_admin_gets_no_ban_button_at_all() -> None:
    target = SimpleNamespace(id=5, status=UserStatus.ACTIVE)

    markup = _detail_keyboard(target, 1, target_is_admin=True)

    assert not any(c and c.startswith("auser:ban") for c in _callbacks(markup)), (
        "the button must not merely fail when pressed — it must not be offered"
    )
    assert any("cannot be banned" in label for label in _labels(markup))


def test_a_banned_admin_can_still_be_unbanned() -> None:
    """A flag set before admins were immune has to stay clearable, or the profile is stuck."""
    target = SimpleNamespace(id=5, status=UserStatus.BANNED)

    markup = _detail_keyboard(target, 1, target_is_admin=True)

    assert any(c and c.startswith("auser:unban") for c in _callbacks(markup))
