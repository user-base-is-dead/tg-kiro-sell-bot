from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.database.models.user import User
from app.locales.i18n import t

router = Router(name="user.help")


@router.message(Command("help"))
async def cmd_help(message: Message, user: User) -> None:
    await message.answer(t("help.body", user.locale))
