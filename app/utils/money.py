from __future__ import annotations


def format_minor(amount_minor: int, currency: str) -> str:
    """Integer minor units -> display string. No floats involved at any point."""
    sign = "-" if amount_minor < 0 else ""
    major, minor = divmod(abs(amount_minor), 100)
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}.get(currency, currency + " ")
    return f"{sign}{symbol}{major}.{minor:02d}"


def parse_to_minor(text: str) -> int:
    """'9.99' -> 999. Raises ValueError on anything that isn't a plain decimal amount."""
    text = text.strip().replace(",", "")
    if not text:
        raise ValueError("empty amount")
    negative = text.startswith("-")
    text = text.removeprefix("-")
    if "." in text:
        major_s, minor_s = text.split(".", 1)
        minor_s = (minor_s + "00")[:2]
    else:
        major_s, minor_s = text, "00"
    if not major_s.isdigit() or not minor_s.isdigit():
        raise ValueError(f"not a valid amount: {text!r}")
    value = int(major_s) * 100 + int(minor_s)
    return -value if negative else value
