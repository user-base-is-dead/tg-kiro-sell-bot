from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.callbacks import NavCB
from app.bot.keyboards.styles import DANGER, PRIMARY, SUCCESS, btn
from app.locales.i18n import t


def nav_row(locale: str, *, back_target: str | None = None, home: bool = True) -> list[InlineKeyboardButton]:
    """Shared [ 🔙 Back ] [ 🏠 Home ] row. back_target is a short nav token (a domain:action
    string or an InteractionState id) resolved by the router — never raw, untrusted state.

    Back is red and Home is blue so the exit row reads the same on every screen in the bot."""
    row: list[InlineKeyboardButton] = []
    if back_target:
        row.append(btn(t("menu.back", locale), NavCB(target=back_target).pack(), DANGER))
    if home:
        row.append(btn(t("menu.home", locale), NavCB(target="home").pack(), PRIMARY))
    return row


def back_keyboard(locale: str, *, target: str = "home") -> InlineKeyboardMarkup:
    """A screen whose only exit is `[ 🔙 Back ]`. Every menu destination gets one, so no screen —
    including the text-only ones and the ones waiting on a form reply — is a dead end the user can
    only leave by pressing the panel or retyping /start. Top-level screens go back to the main
    menu, which is also Home, so they don't carry a second button that does the same thing."""
    return with_nav([], locale, back_target=target, home=False)


def confirm_row(locale: str, confirm_cb: str, cancel_cb: str) -> list[InlineKeyboardButton]:
    return [
        btn(t("menu.confirm", locale), confirm_cb, SUCCESS),
        btn(t("menu.cancel", locale), cancel_cb, DANGER),
    ]


def with_nav(
    rows: list[list[InlineKeyboardButton]],
    locale: str,
    *,
    back_target: str | None = None,
    home: bool = True,
) -> InlineKeyboardMarkup:
    nav = nav_row(locale, back_target=back_target, home=home)
    if nav:
        rows = [*rows, nav]
    return InlineKeyboardMarkup(inline_keyboard=rows)
