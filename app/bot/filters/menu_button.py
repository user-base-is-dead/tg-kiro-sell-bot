from __future__ import annotations

from typing import Any

from aiogram.filters import Filter
from aiogram.types import Message

from app.locales.i18n import t


class MenuButton(Filter):
    """Matches a reply-keyboard press against the localized label for an i18n key, using the
    current user's locale — so the same physical button works after a language switch."""

    def __init__(self, key: str) -> None:
        self.key = key

    async def __call__(self, message: Message, **data: Any) -> bool:
        user = data.get("user")
        locale = user.locale if user else "en"
        return message.text == t(self.key, locale)
