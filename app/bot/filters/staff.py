"""Who counts as staff inside the support group and the orders group.

Those two groups are where the team works: every ticket and every order has its own topic there.
Membership is the staff list — whoever the owner adds to a group can reply to buyers, close topics,
settle warranty claims and fulfil or decline orders from inside it, without an ADMIN_IDS entry.
Both groups are private, so only members can post in them or press the buttons in them, which makes
the chat an update comes from the credential.

Everything else — the admin panel, settling or unfreezing refund money, users, products,
broadcasts — stays behind IsAdmin, in the groups and out of them.
"""

from __future__ import annotations

from typing import Any

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.filters.is_admin import IsAdmin
from app.core.config import get_settings


def is_staff_group(chat_id: int | None) -> bool:
    """SUPPORT_GROUP_ID or ORDERS_GROUP_ID — the two chats whose members are staff."""
    if chat_id is None:
        return False
    settings = get_settings()
    return chat_id in (settings.support_group_id, settings.orders_group_id)


def sent_by_a_person(message: Message) -> bool:
    """A human typing as themselves.

    Not other bots in the group, and not posts made on behalf of a chat — an anonymous admin, a
    channel, a channel's automatic forward. Telegram hides who actually wrote those, so there is
    nobody to credit the reply to.
    """
    sender = message.from_user
    return message.sender_chat is None and sender is not None and not sender.is_bot


class InStaffGroup(Filter):
    """A person acting inside the support or orders group."""

    async def __call__(self, event: TelegramObject, **_: Any) -> bool:
        if isinstance(event, Message):
            return sent_by_a_person(event) and is_staff_group(event.chat.id)
        if isinstance(event, CallbackQuery):
            # Only people press buttons, so there is no sender to rule out — just the chat.
            chat = event.message.chat if event.message is not None else None
            return chat is not None and is_staff_group(chat.id)
        return False


class IsStaff(Filter):
    """Anyone inside the staff groups, and admins anywhere — an admin can still do the same work
    from the bot's private chat."""

    async def __call__(self, event: TelegramObject, **data: Any) -> bool:
        if await InStaffGroup()(event, **data):
            return True
        return await IsAdmin()(event, **data)
