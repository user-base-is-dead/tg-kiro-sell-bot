from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat

logger = logging.getLogger(__name__)

# Registered on the default scope, so every chat gets these regardless of who's on the other end.
USER_COMMANDS = [
    BotCommand(command="start", description="Main menu"),
    BotCommand(command="products", description="Browse the store"),
    BotCommand(command="orders", description="Your order history"),
    BotCommand(command="support", description="Get help from our team"),
    BotCommand(command="language", description="Change language"),
    BotCommand(command="gift", description="Redeem a gift code"),
    BotCommand(command="refer", description="Your referral link"),
    BotCommand(command="warranty", description="Warranty status"),
    BotCommand(command="topup", description="Top up your balance"),
    BotCommand(command="wallet", description="Your wallet history"),
    BotCommand(command="profile", description="Your account"),
    BotCommand(command="help", description="Help"),
]

# Layered on top of USER_COMMANDS, per admin chat (see register_bot_commands) — never registered
# under BotCommandScopeAllChatAdministrators, which means administrators of a group/supergroup
# chat. This bot only ever talks in private chats, so that scope never matches anyone and would
# leave these permanently invisible no matter who is in ADMIN_IDS.
ADMIN_COMMANDS = [
    BotCommand(command="admin", description="Admin panel"),
    BotCommand(command="adjust_balance", description="Adjust user balance"),
    BotCommand(command="pending_orders", description="Orders awaiting fulfilment"),
    BotCommand(command="refund_wallets", description="Refunds waiting to be settled"),
    BotCommand(command="open_tickets", description="Support queue"),
]


async def register_bot_commands(bot: Bot, admin_ids: list[int]) -> None:
    """Sets USER_COMMANDS as the default, then gives each admin's private chat the combined list.
    A chat-scoped command list replaces the default rather than merging with it, so the per-admin
    list has to repeat USER_COMMANDS alongside ADMIN_COMMANDS or admins would lose the regular
    menu entries."""
    await bot.set_my_commands(USER_COMMANDS)
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(USER_COMMANDS + ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramAPIError as exc:
            # Most often "chat not found": this admin id is in ADMIN_IDS but has never opened a
            # chat with the bot yet. Skip it rather than aborting the rest of the admin list.
            logger.warning("Could not set admin commands for %s: %s", admin_id, exc)


async def clear_admin_commands(bot: Bot, telegram_ids: list[int]) -> None:
    """Drops a revoked admin's chat-scoped command list so they fall back to seeing USER_COMMANDS,
    instead of Telegram still offering /admin from a scope that's now stale."""
    for telegram_id in telegram_ids:
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=telegram_id))
        except TelegramAPIError as exc:
            logger.warning("Could not clear admin commands for %s: %s", telegram_id, exc)
