from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape
from typing import NamedTuple

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import new_ticket_number
from app.database.models.support import SupportTicket, TicketStatus
from app.database.models.user import User
from app.database.repositories.support_repo import SupportRepo
from app.database.repositories.user_repo import UserRepo
from app.locales.i18n import t
from app.utils.errors import UserError

logger = logging.getLogger(__name__)


async def _send_media(bot: Bot, chat_id: int, media_id: str, message_thread_id: int | None = None) -> None:
    """Send media (stored as 'type:file_id') to a chat using the appropriate Telegram method."""
    if ":" not in media_id:
        media_id = f"document:{media_id}"

    media_type, file_id = media_id.split(":", 1)

    try:
        if media_type == "photo":
            await bot.send_photo(chat_id, file_id, message_thread_id=message_thread_id)
        elif media_type == "video":
            await bot.send_video(chat_id, file_id, message_thread_id=message_thread_id)
        elif media_type == "voice":
            await bot.send_voice(chat_id, file_id, message_thread_id=message_thread_id)
        elif media_type == "audio":
            await bot.send_audio(chat_id, file_id, message_thread_id=message_thread_id)
        elif media_type == "video_note":
            await bot.send_video_note(chat_id, file_id, message_thread_id=message_thread_id)
        else:
            await bot.send_document(chat_id, file_id, message_thread_id=message_thread_id)
    except TelegramAPIError as exc:
        logger.error("Failed to send media to chat %s (%s)", chat_id, exc)


def _field(value: object | None, label: str) -> str:
    """Support staff act on the identity block, so a blank field must be loud rather than absent —
    an empty line reads as "no username" and as "the bot dropped it" alike. Telegram genuinely
    omits `username` for accounts that never set one, so this is a normal state, not an error.
    Escaped because a display name may legitimately contain < or &, which would otherwise break
    the whole HTML message and take the identity block down with it."""
    text = str(value).strip() if value is not None else ""
    return escape(text) if text else f"[{label} MISSING]"


def _identity_block(user: User) -> str:
    full_name = " ".join(part for part in (user.first_name, user.last_name) if part)
    username = f"@{escape(user.username)}" if user.username else "[USERNAME MISSING]"
    return (
        f"👤 Username: {username}\n"
        f"🆔 User ID: {_field(user.telegram_id, 'USER ID')}\n"
        f"📝 Name: {_field(full_name, 'NAME')}"
    )


def format_ticket_header(*, ticket_number: str, category: str, user: User) -> str:
    """Posted into the topic *before* the user's own first message, so staff always open a thread
    onto who they are talking to rather than onto a bare line of text."""
    return (
        f"🎫 <b>{_field(ticket_number, 'TICKET ID')}</b>\n"
        f"📂 Category: {_field(category, 'CATEGORY')}\n\n"
        f"{_identity_block(user)}"
    )


def format_topic_name(ticket_number: str, user: User) -> str:
    """Telegram caps forum topic names at 128 chars. The ticket id leads because that is the
    handle staff quote back; the username is the human-readable half."""
    who = f"@{user.username}" if user.username else f"id {user.telegram_id}"
    return f"{ticket_number} · {who}"[:128]


class NewTicket(NamedTuple):
    """`reached_staff` is false when the ticket is safely in the database but no human was
    actually told about it — a misconfigured SUPPORT_GROUP_ID, the bot removed from the group.
    Callers must not report success to the user on the strength of the row alone."""

    ticket: SupportTicket
    reached_staff: bool


async def _notify_admins(bot: Bot, text: str) -> bool:
    """Last-resort delivery when the support group is unreachable, so a ticket is never silently
    lost. Uses the ADMIN_IDS floor from env rather than the admins table: the table is what a
    broken deployment is most likely to have gotten wrong."""
    delivered = False
    for admin_id in get_settings().admin_ids:
        try:
            await bot.send_message(admin_id, text)
            delivered = True
        except TelegramAPIError as exc:
            logger.warning("Admin fallback DM to %s failed (%s)", admin_id, exc)
    return delivered


async def create_ticket(
    bot: Bot, session: AsyncSession, *, user: User, category: str, subject: str, support_group_id: int | None
) -> NewTicket:
    now = datetime.now(UTC)
    topic_id: int | None = None

    # The number is minted before the topic, not after, because it is what names the topic — the
    # old order forced the name to fall back to the raw telegram id.
    ticket_number = new_ticket_number()

    if support_group_id is not None:
        try:
            topic = await bot.create_forum_topic(
                chat_id=support_group_id, name=format_topic_name(ticket_number, user)
            )
            topic_id = topic.message_thread_id
        except TelegramAPIError as exc:
            logger.warning(
                "Couldn't create forum topic in SUPPORT_GROUP_ID=%s (%s) — falling back to root chat.",
                support_group_id,
                exc,
            )

    ticket = SupportTicket(
        ticket_number=ticket_number,
        user_id=user.id,
        category=category,
        subject=subject,
        status=TicketStatus.OPEN,
        topic_id=topic_id,
        opened_at=now,
    )
    session.add(ticket)
    await session.flush()

    repo = SupportRepo(session)
    await repo.add_message(
        ticket_id=ticket.id, author_type="USER", author_telegram_id=user.telegram_id, content=subject, created_at=now
    )

    header = format_ticket_header(ticket_number=ticket.ticket_number, category=category, user=user)

    if support_group_id is None:
        logger.error("SUPPORT_GROUP_ID is unset — ticket %s has nowhere to go.", ticket.ticket_number)
        body = f"⚠️ SUPPORT_GROUP_ID is unset.\n\n{header}\n\n{escape(subject)}"
        return NewTicket(ticket, await _notify_admins(bot, body))

    try:
        # Identity card first, the user's own words second — two messages, so the opening message
        # of the thread reads exactly like every follow-up that lands under it.
        await bot.send_message(support_group_id, header, message_thread_id=topic_id)
        await bot.send_message(support_group_id, escape(subject), message_thread_id=topic_id)
    except TelegramAPIError as exc:
        logger.error(
            "Ticket %s created but NOT delivered to SUPPORT_GROUP_ID=%s (%s) — falling back to admin DMs.",
            ticket.ticket_number,
            support_group_id,
            exc,
        )
        body = f"⚠️ Support group unreachable ({escape(str(exc))}).\n\n{header}\n\n{escape(subject)}"
        return NewTicket(ticket, await _notify_admins(bot, body))

    return NewTicket(ticket, True)


async def relay_user_message(
    bot: Bot,
    session: AsyncSession,
    *,
    ticket: SupportTicket,
    user: User,
    text: str | None = None,
    attachment_file_ids: list[str] | None = None,
) -> None:
    if ticket.status not in (TicketStatus.OPEN, TicketStatus.PENDING):
        raise UserError("support.ticket_closed")

    attachment_file_ids = attachment_file_ids or []
    now = datetime.now(UTC)
    await SupportRepo(session).add_message(
        ticket_id=ticket.id,
        author_type="USER",
        author_telegram_id=user.telegram_id,
        content=text,
        attachment_file_ids=attachment_file_ids,
        created_at=now,
    )

    settings = get_settings()
    if settings.support_group_id is not None:
        try:
            if attachment_file_ids:
                for media_id in attachment_file_ids:
                    await _send_media(bot, settings.support_group_id, media_id, message_thread_id=ticket.topic_id)
            if text:
                await bot.send_message(settings.support_group_id, escape(text), message_thread_id=ticket.topic_id)
        except TelegramAPIError as exc:
            logger.error(
                "Couldn't relay message to SUPPORT_GROUP_ID=%s for ticket %s (%s)",
                settings.support_group_id,
                ticket.ticket_number,
                exc,
            )


async def relay_staff_message(
    bot: Bot,
    session: AsyncSession,
    *,
    ticket: SupportTicket,
    staff_telegram_id: int,
    text: str | None = None,
    attachment_file_ids: list[str] | None = None,
) -> None:
    attachment_file_ids = attachment_file_ids or []
    now = datetime.now(UTC)
    await SupportRepo(session).add_message(
        ticket_id=ticket.id,
        author_type="STAFF",
        author_telegram_id=staff_telegram_id,
        content=text,
        attachment_file_ids=attachment_file_ids,
        created_at=now,
    )

    buyer = await UserRepo(session).get_by_id(ticket.user_id)
    if buyer and buyer.chat_id:
        try:
            if attachment_file_ids:
                for media_id in attachment_file_ids:
                    await _send_media(bot, buyer.chat_id, media_id)
            if text:
                await bot.send_message(buyer.chat_id, f"💬 <b>Support:</b>\n\n{escape(text)}")
        except TelegramAPIError:
            logger.warning("Couldn't DM user for ticket %s (blocked?)", ticket.ticket_number)


async def close_ticket(session: AsyncSession, *, ticket_id: int, reason: str) -> SupportTicket:
    ticket = await SupportRepo(session).get_by_id(ticket_id)
    if ticket is None:
        raise UserError("common.unknown_action")
    ticket.status = TicketStatus.CLOSED
    ticket.closed_at = datetime.now(UTC)
    ticket.close_reason = reason
    await session.flush()
    return ticket


async def _close_topic(bot: Bot, ticket: SupportTicket) -> None:
    """Retire the forum thread so staff can't keep typing into something nobody reads. Best-effort:
    a lost topic must not roll back a close that is already done as far as the caller is concerned."""
    group_id = get_settings().support_group_id
    if group_id is None or ticket.topic_id is None:
        return
    try:
        await bot.close_forum_topic(chat_id=group_id, message_thread_id=ticket.topic_id)
    except TelegramAPIError as exc:
        logger.warning("Couldn't close topic %s for %s (%s)", ticket.topic_id, ticket.ticket_number, exc)


async def close_claim_ticket(bot: Bot, session: AsyncSession, *, ticket_id: int | None, reason: str) -> None:
    """Close the ticket a warranty claim was discussed in, as part of resolving the claim itself.

    A warranty claim's thread exists only for that claim, so /done, /reject and the auto-reject job
    are the real end of it — requiring a separate /close leaves dead threads open and lets the user
    keep writing into a claim that is already decided. No closure notice is sent here: the caller
    has already told the user the outcome, which is strictly more informative.
    """
    if ticket_id is None:
        return
    ticket = await SupportRepo(session).get_by_id(ticket_id)
    if ticket is None or ticket.status is TicketStatus.CLOSED:
        return
    await close_ticket(session, ticket_id=ticket.id, reason=reason)
    await _close_topic(bot, ticket)


async def announce_closure(bot: Bot, session: AsyncSession, ticket: SupportTicket) -> None:
    """The two things that must happen outside the database whenever a ticket closes, wherever the
    close came from: tell the user in their own language, and close the Telegram topic so staff
    can't keep typing into a thread nobody is reading any more.

    Both are best-effort — a blocked user or a lost topic must not roll back the close itself,
    which is already committed as far as the caller is concerned."""
    buyer = await UserRepo(session).get_by_id(ticket.user_id)
    if buyer and buyer.chat_id:
        try:
            await bot.send_message(buyer.chat_id, t("support.closed_notice", buyer.locale))
        except TelegramAPIError as exc:
            logger.warning("Couldn't tell user about closure of %s (%s)", ticket.ticket_number, exc)

    await _close_topic(bot, ticket)
