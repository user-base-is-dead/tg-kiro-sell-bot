from __future__ import annotations

from typing import Final

from aiogram.types import CopyTextButton, InlineKeyboardButton

# Bot API 9.4 (February 2026) added `style` to InlineKeyboardButton / KeyboardButton, changing the
# button's color instead of the flat theme default. The exact rendering is up to each client: on
# Telegram Desktop (observed 2026-08) a styled button is an outlined box at rest and only fills
# solid blue while being pressed, so don't expect a permanent solid-fill look on every client.
# Only these three values are accepted — anything else is rejected by the API, so they live here as
# constants and every keyboard picks one of them rather than passing a raw string.
PRIMARY: Final = "primary"  # blue  — navigation and neutral primary actions
SUCCESS: Final = "success"  # green — money-in, confirm, approve, create
DANGER: Final = "danger"  # red   — cancel, delete, reject, leave a screen

# A button with no style keeps Telegram's default transparent background. Used for inert filler
# (page indicators) and long scrollable lists, where a wall of solid color reads as noise.
NEUTRAL: Final = None


def btn(text: str, callback_data: str, style: str | None = NEUTRAL) -> InlineKeyboardButton:
    """Every styled inline button in the bot goes through here, so the `style` argument can never
    drift into a value Telegram would reject."""
    if style not in (PRIMARY, SUCCESS, DANGER, None):
        raise ValueError(f"unsupported button style: {style!r}")
    return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)


def copy_btn(text: str, value: str, style: str | None = PRIMARY) -> InlineKeyboardButton:
    """A button that puts `value` on the buyer's clipboard, client-side, with no round trip.

    Worth a button rather than leaving it to tap-to-copy on a <code> span: the copy target here is a
    payment amount whose last decimals are what identify the payer, and a partial selection or a
    hand-typed digit is a payment nobody can attribute. Telegram guarantees the whole string.
    """
    if style not in (PRIMARY, SUCCESS, DANGER, None):
        raise ValueError(f"unsupported button style: {style!r}")
    return InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=value), style=style)


def url_btn(text: str, url: str, style: str | None = PRIMARY) -> InlineKeyboardButton:
    if style not in (PRIMARY, SUCCESS, DANGER, None):
        raise ValueError(f"unsupported button style: {style!r}")
    return InlineKeyboardButton(text=text, url=url, style=style)
