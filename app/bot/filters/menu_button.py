from __future__ import annotations

from typing import Any

from aiogram.filters import Filter
from aiogram.types import Message

from app.locales.i18n import t

# Labels the retired reply-keyboard panel used to print, before the PowerX rebrand renamed them.
# A client that has not been sent the panel removal yet still shows that panel and still sends these
# exact strings, so each one keeps opening the screen it always opened. Only keys whose label
# actually changed need an entry.
LEGACY_LABELS: dict[str, frozenset[str]] = {
    "menu.products": frozenset({"🛍️ Products"}),
    "menu.orders": frozenset({"📦 Orders"}),
    "menu.support": frozenset({"💬 Live Chat"}),
    "menu.warranty": frozenset({"🔧 Warranty"}),
    # 👤 Profile's screen became 👛 Wallet, so the old press opens the new screen. 💳 Top Up is gone
    # with nothing to replace it, so it is not listed: that text now reads as an ordinary message.
    "menu.wallet": frozenset({"👤 Profile"}),
    "menu.admin_panel": frozenset({"🛡️ Admin Panel"}),
}


def menu_labels(key: str, locale: str) -> set[str]:
    """Every text that counts as a press of the `key` button: today's label plus any retired one."""
    return {t(key, locale), *LEGACY_LABELS.get(key, ())}


class MenuButton(Filter):
    """Matches a reply-keyboard press against the localized label for an i18n key, using the
    current user's locale — so the same physical button works after a language switch, and after
    a rename."""

    def __init__(self, key: str) -> None:
        self.key = key

    async def __call__(self, message: Message, **data: Any) -> bool:
        user = data.get("user")
        locale = user.locale if user else "en"
        return message.text in menu_labels(self.key, locale)
