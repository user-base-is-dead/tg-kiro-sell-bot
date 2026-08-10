from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.callbacks import LangCB, NavCB
from app.bot.keyboards.common import with_nav
from app.bot.keyboards.styles import PRIMARY, btn
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

# The inline menu is one flat blue.
_STYLE = dict.fromkeys(_MENU_TARGETS, PRIMARY)


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


# There is no reply-keyboard panel any more — see `app.bot.panel`. The `MenuButton` filter and its
# handlers stay: a client that still shows the old panel keeps working until the removal reaches it.


def language_inline_keyboard(locale: str = "en") -> InlineKeyboardMarkup:
    rows = [
        [btn(_LOCALE_LABEL[loc], LangCB(locale=loc).pack(), PRIMARY)] for loc in supported_locales()
    ]
    return with_nav(rows, locale, back_target="home", home=False)
