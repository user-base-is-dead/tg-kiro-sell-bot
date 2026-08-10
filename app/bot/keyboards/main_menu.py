from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.bot.callbacks import LangCB, NavCB
from app.bot.keyboards.common import with_nav
from app.bot.keyboards.styles import NEUTRAL, PRIMARY, btn
from app.core.config import get_settings
from app.locales.i18n import supported_locales, t

_LOCALE_LABEL = {"en": "🇬🇧 English"}

_MENU_TARGETS = (
    "categories",
    "orders",
    "profile",
    "topup",
    "warranty",
    "refer",
    "gift",
    "support",
    "admin_panel",
)

# The inline menu is blue. The bottom panel is deliberately left unstyled: clients paint reply
# labels in the theme accent (blue) whatever `style` says, and `style` offers only
# primary/success/danger — no colour among them that a blue label sits well on. Default white it is.
_STYLE = dict.fromkeys(_MENU_TARGETS, PRIMARY)
_REPLY_STYLE: str | None = NEUTRAL


def main_inline_keyboard(locale: str, *, is_admin: bool) -> InlineKeyboardMarkup:
    """Main menu rendered as buttons attached to the message itself, so they're visible in the
    chat without the user needing to open a separate reply-keyboard panel. The admin row is
    appended only when `is_admin` is true at render time — never a static keyboard baked in once
    and reused."""

    def _btn(key: str, target: str) -> InlineKeyboardButton:
        return btn(t(key, locale), NavCB(target=target).pack(), _STYLE[target])

    rows = [
        [_btn("menu.products", "categories"), _btn("menu.orders", "orders")],
        [_btn("menu.profile", "profile"), _btn("menu.topup", "topup")],
        [_btn("menu.gift", "gift"), _btn("menu.refer", "refer")],
        [_btn("menu.warranty", "warranty"), _btn("menu.support", "support")],
    ]
    # A url button, not a callback one: it opens the group directly instead of costing the user a
    # round trip through the bot. Skipped when no group is configured, so the row is never dead.
    group_url = get_settings().community_group_url.strip()
    if group_url:
        rows.append(
            [InlineKeyboardButton(text=t("menu.community", locale), url=group_url, style=PRIMARY)]
        )
    if is_admin:
        rows.append([_btn("menu.admin_panel", "admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# The same entries as the inline menu, in the same order, so the bottom panel and the in-message
# menu never look like two different products. `style` works here too (Bot API 9.4) — a reply
# button's unstyled default is white rather than transparent.
#
# The one exception is the leading Start row: the panel is the only surface visible from *inside*
# another screen (or a half-finished form), so it carries the way back to the top. The inline menu
# is that top, and would be linking to itself.
_REPLY_LAYOUT: list[list[str]] = [
    ["menu.start"],
    ["menu.products", "menu.orders"],
    ["menu.profile", "menu.topup"],
    ["menu.gift", "menu.refer"],
    ["menu.warranty", "menu.support"],
]


def main_reply_keyboard(locale: str, *, is_admin: bool) -> ReplyKeyboardMarkup:
    """Persistent bottom panel. Its buttons send their own localized label as plain text, which the
    `MenuButton` filter matches back to an i18n key — so it must be re-sent whenever the user's
    locale changes, or the old labels stop matching."""
    rows = [
        [KeyboardButton(text=t(key, locale), style=_REPLY_STYLE) for key in row]
        for row in _REPLY_LAYOUT
    ]
    if is_admin:
        rows.append([KeyboardButton(text=t("menu.admin_panel", locale), style=_REPLY_STYLE)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def language_inline_keyboard(locale: str = "en") -> InlineKeyboardMarkup:
    rows = [
        [btn(_LOCALE_LABEL[loc], LangCB(locale=loc).pack(), PRIMARY)] for loc in supported_locales()
    ]
    return with_nav(rows, locale, back_target="home", home=False)
