"""Print chat IDs (and topic IDs) from recent bot updates.

One-off helper for filling SUPPORT_GROUP_ID / LOG_CHAT_ID in .env.

Telegram allows only one getUpdates consumer per token, so STOP the bot before
running this — otherwise you get 409 Conflict and the running bot silently eats
the very messages you're trying to inspect.

    python -m scripts.get_chat_id

Then send any message in the target group and re-run.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot

from app.core.config import get_settings


async def main() -> None:
    bot = Bot(get_settings().bot_token)
    try:
        updates = await bot.get_updates(timeout=0, limit=100)
    finally:
        await bot.session.close()

    if not updates:
        print("No pending updates. Send a message in the group, then run this again.")
        print("(If the group is a forum, send it inside a topic to also capture the topic id.)")
        return

    seen: dict[int, str] = {}
    for update in updates:
        msg = update.message or update.edited_message or update.channel_post
        if msg is None:
            continue
        chat = msg.chat
        thread = f"  topic_id={msg.message_thread_id}" if msg.message_thread_id else ""
        seen[chat.id] = f"{chat.type:<10} {chat.title or chat.full_name!r}{thread}"

    for chat_id, label in seen.items():
        print(f"{chat_id:>16}   {label}")

    print("\nSupergroup/channel IDs start with -100. Paste into .env, then restart the bot.")


if __name__ == "__main__":
    asyncio.run(main())
