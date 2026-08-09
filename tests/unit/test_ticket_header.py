from __future__ import annotations

import pytest

from app.database.models.user import User
from app.services.support_service import format_ticket_header, format_topic_name


def _user(**overrides) -> User:
    fields = {
        "telegram_id": 8819820836,
        "username": "shopgabotadmin",
        "first_name": "Vivek",
        "last_name": None,
        "locale": "en",
    }
    return User(**{**fields, **overrides})


def _header(**overrides) -> str:
    return format_ticket_header(ticket_number="TCK-A4BD2C", category="General", user=_user(**overrides))


def test_header_carries_ticket_id_username_and_user_id() -> None:
    header = _header()
    assert "TCK-A4BD2C" in header
    assert "@shopgabotadmin" in header
    assert "8819820836" in header


def test_missing_username_is_called_out_not_dropped() -> None:
    """Telegram omits `username` for accounts that never set one. The old header silently
    substituted the numeric id, so staff could not tell "no username" from "field lost"."""
    header = _header(username=None)
    assert "[USERNAME MISSING]" in header
    assert "8819820836" in header  # the id is still its own field, not a stand-in


@pytest.mark.parametrize(
    ("overrides", "marker"),
    [
        ({"username": None}, "[USERNAME MISSING]"),
        ({"first_name": None, "last_name": None}, "[NAME MISSING]"),
    ],
)
def test_each_absent_field_gets_its_own_marker(overrides: dict, marker: str) -> None:
    assert marker in _header(**overrides)


def test_missing_category_is_marked() -> None:
    assert "[CATEGORY MISSING]" in format_ticket_header(ticket_number="T-1", category="", user=_user())


def test_full_name_joins_both_parts() -> None:
    assert "Vivek Kumar" in _header(first_name="Vivek", last_name="Kumar")


def test_html_in_a_display_name_cannot_break_the_message() -> None:
    """Unescaped, this makes Telegram reject the whole sendMessage — and the ticket notification
    disappears with it, which is the exact failure mode this bot already shipped once."""
    header = _header(first_name="<b>bold", last_name=None)
    assert "&lt;b&gt;bold" in header
    assert "<b>bold" not in header


def test_topic_name_leads_with_the_ticket_id() -> None:
    name = format_topic_name("TCK-A4BD2C", _user())
    assert name.startswith("TCK-A4BD2C")
    assert "@shopgabotadmin" in name


def test_topic_name_falls_back_to_the_id_without_a_username() -> None:
    assert format_topic_name("TCK-A4BD2C", _user(username=None)) == "TCK-A4BD2C · id 8819820836"


def test_topic_name_stays_within_telegrams_128_char_cap() -> None:
    assert len(format_topic_name("TCK-A4BD2C", _user(username="u" * 200))) <= 128
