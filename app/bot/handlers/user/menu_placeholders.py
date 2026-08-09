from __future__ import annotations

from aiogram import Router
from aiogram.types import Message

from app.bot.filters.menu_button import MenuButton
from app.database.models.user import User

router = Router(name="user.menu_placeholders")

# Each of these ships a real screen in its own phase (see ARCHITECTURE.md §8). Until then the
# button is live and localized, but honestly says "not yet" instead of doing nothing.
_PLACEHOLDERS: dict[str, str] = {}


def _make_handler(text: str):
    async def _handler(message: Message, user: User) -> None:  # noqa: ARG001
        await message.answer(text)

    return _handler


for _key, _text in _PLACEHOLDERS.items():
    router.message(MenuButton(_key))(_make_handler(_text))
