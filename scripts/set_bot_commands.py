"""Registers the visible command list via setMyCommands without restarting the bot process.
app/main.py already re-registers commands on every startup, so this is only needed to push a
command update (e.g. a new description) live in between restarts."""
from __future__ import annotations

import asyncio

from aiogram import Bot

from app.bot.commands import register_bot_commands
from app.core.config import get_settings


async def main() -> None:
    settings = get_settings()
    bot = Bot(token=settings.bot_token)

    await register_bot_commands(bot, settings.admin_ids)
    print(f"Registered the default command list and {len(settings.admin_ids)} admin-scoped list(s).")

    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
