from __future__ import annotations

import pytest

from app.utils.money import format_minor, parse_to_minor


def test_parse_to_minor_basic():
    assert parse_to_minor("9.99") == 999
    assert parse_to_minor("10") == 1000
    assert parse_to_minor("0.05") == 5
    assert parse_to_minor("1,234.56") == 123456


def test_parse_to_minor_negative():
    assert parse_to_minor("-5.50") == -550


def test_parse_to_minor_accepts_an_explicit_plus():
    """`-` parsed but `+` raised, so /adjust_balance — whose own usage text reads
    `+10.00` — could only ever debit. Every credit answered "Invalid amount"."""
    assert parse_to_minor("+5.50") == 550
    assert parse_to_minor("+10") == 1000
    assert parse_to_minor("+0.05") == 5


def test_parse_to_minor_rejects_garbage():
    for bad in ("", "abc", "1.2.3", "--5", "++5", "+-5", "-+5", "+", "-"):
        with pytest.raises(ValueError):
            parse_to_minor(bad)


def test_format_minor_round_trip():
    assert format_minor(999, "USD") == "$9.99"
    assert format_minor(-550, "USD") == "-$5.50"
    assert format_minor(100000, "INR") == "₹1000.00"
    assert format_minor(500, "XYZ") == "XYZ 5.00"
