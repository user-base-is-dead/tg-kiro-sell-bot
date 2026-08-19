from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters.is_admin import is_admin_user
from app.core.config import get_settings
from app.database.models.support import TicketStatus
from app.database.models.user import User
from app.database.repositories.support_repo import SupportRepo
from app.locales.i18n import t
from app.services.support_service import relay_staff_message, relay_user_message
from app.utils.errors import UserError

# Registered LAST in main.py — this only ever sees a DM/group message that no earlier,
# more specific handler (commands, menu buttons, FSM wizard steps) already claimed. That's
# what makes "just reply to chat with support" work without a dedicated keyboard button.

router = Router(name="support.relay")


def _extract_attachments(message: Message) -> tuple[str | None, list[str]]:
    """Extract text and file IDs (photos, videos, documents, voice messages, etc.) from a message.
    Returns (text, file_ids) where file_ids are stored as "type:file_id" for proper relay."""
    text = message.text or message.caption
    file_ids = []

    if message.photo:
        file_ids.append(f"photo:{message.photo[-1].file_id}")
    if message.video:
        file_ids.append(f"video:{message.video.file_id}")
    if message.video_note:
        file_ids.append(f"video_note:{message.video_note.file_id}")
    if message.voice:
        file_ids.append(f"voice:{message.voice.file_id}")
    if message.audio:
        file_ids.append(f"audio:{message.audio.file_id}")
    if message.document:
        file_ids.append(f"document:{message.document.file_id}")

    return text, file_ids


async def dm_relay(message: Message, session: AsyncSession, user: User | None = None) -> None:
    """Any DM that fell through everything else: if the user has an open ticket,
    treat it as a reply to support. Supports text, photos, and documents."""
    if user is None or message.chat.type != "private":
        return

    text, file_ids = _extract_attachments(message)
    if not text and not file_ids:
        return

    ticket = await SupportRepo(session).get_open_for_user(user.id)
    if ticket is None:
        return

    try:
        await relay_user_message(message.bot, session, ticket=ticket, user=user, text=text, attachment_file_ids=file_ids)
    except UserError as exc:
        await message.answer(t(exc.i18n_key, user.locale, **exc.vars))


async def group_relay(message: Message, session: AsyncSession, user: User | None = None) -> None:
    """Any message inside a ticket's forum topic, from a staff member, gets mirrored to the ticket
    owner's DM. Supports text, photos, and documents.

    Two groups qualify: SUPPORT_GROUP_ID, and ORDERS_GROUP_ID for a cancelled order whose thread has
    become a dispute the buyer is connected to. Topic numbers are per-chat, so which group the message
    came from is part of identifying the ticket — matching on the topic id alone would eventually
    relay a reply to the wrong buyer.
    """
    if user is None:
        return
    settings = get_settings()
    in_support = settings.support_group_id is not None and message.chat.id == settings.support_group_id
    in_orders = settings.orders_group_id is not None and message.chat.id == settings.orders_group_id
    if not (in_support or in_orders):
        return
    if message.message_thread_id is None:
        return

    text, file_ids = _extract_attachments(message)
    if not text and not file_ids:
        return

    # Commands are staff talking to the bot, not to the buyer.
    if text and text.startswith("/"):
        return
    if not await is_admin_user(session, user.telegram_id):
        return

    ticket = await SupportRepo(session).get_by_topic_in_group(
        message.message_thread_id, message.chat.id, is_support_group=in_support
    )
    if ticket is None:
        return
    # An order thread that nobody has connected the buyer to is a log, not a conversation: staff talk
    # about the order in it all the time, and mirroring that to the buyer would leak internal notes.
    if in_orders and ticket.status not in (TicketStatus.OPEN, TicketStatus.PENDING):
        return

    await relay_staff_message(message.bot, session, ticket=ticket, staff_telegram_id=user.telegram_id, text=text, attachment_file_ids=file_ids)


# Both handlers live on one router, and aiogram stops propagating at the FIRST handler whose
# filters pass — regardless of what that handler returns. So a filterless `group_relay` would
# match every DM too, run, return None, and starve `dm_relay` of every message it exists for.
# The chat-type filters keep the two disjoint; the guards inside each handler are then only
# defense in depth.
router.message.register(group_relay, F.chat.type.in_({"group", "supergroup"}))
router.message.register(dm_relay, F.chat.type == "private")
