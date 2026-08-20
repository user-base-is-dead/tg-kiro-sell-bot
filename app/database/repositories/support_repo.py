from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.support import SupportTicket, TicketMessage, TicketStatus


class SupportRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_open_for_user(self, user_id: int) -> SupportTicket | None:
        result = await self._session.execute(
            select(SupportTicket)
            .where(SupportTicket.user_id == user_id, SupportTicket.status.in_([TicketStatus.OPEN, TicketStatus.PENDING]))
            .order_by(SupportTicket.opened_at.desc())
        )
        return result.scalars().first()

    async def get_by_id(self, ticket_id: int) -> SupportTicket | None:
        return await self._session.get(SupportTicket, ticket_id)

    async def get_by_topic_id(self, topic_id: int) -> SupportTicket | None:
        result = await self._session.execute(select(SupportTicket).where(SupportTicket.topic_id == topic_id))
        return result.scalars().first()

    async def get_by_topic_in_group(self, topic_id: int, group_chat_id: int, *, is_support_group: bool) -> SupportTicket | None:
        """The ticket a forum topic belongs to, resolved within one chat.

        Two groups now host ticket topics (SUPPORT_GROUP_ID and, for order disputes, ORDERS_GROUP_ID)
        and Telegram numbers topics per chat — so topic 42 exists in both, meaning two different
        conversations. Matching on `topic_id` alone would relay a staff reply into the wrong buyer's
        chat the first time those numbers collide.

        `is_support_group` covers the rows that predate `group_chat_id`: NULL has always meant the
        support group, and back-filling it would only guess at the id the env held at the time.
        Newest first, because a topic can legitimately be reused after its ticket closes.
        """
        where = SupportTicket.group_chat_id == group_chat_id
        if is_support_group:
            where = or_(where, SupportTicket.group_chat_id.is_(None))
        result = await self._session.execute(
            select(SupportTicket)
            .where(SupportTicket.topic_id == topic_id, where)
            .order_by(SupportTicket.opened_at.desc())
        )
        return result.scalars().first()

    async def get_open_order_dispute(self, user_id: int) -> SupportTicket | None:
        """The live order dispute holding this user, if any — what blocks Create Ticket and Warranty."""
        result = await self._session.execute(
            select(SupportTicket)
            .where(
                SupportTicket.user_id == user_id,
                SupportTicket.order_id.is_not(None),
                SupportTicket.status.in_([TicketStatus.OPEN, TicketStatus.PENDING]),
            )
            .order_by(SupportTicket.opened_at.desc())
        )
        return result.scalars().first()

    async def get_dispute_for_order(self, order_id: str) -> SupportTicket | None:
        """This order's dispute, whatever state it is in. An order has at most one — its number IS
        the ticket number — so a second decline reopens this rather than minting a duplicate."""
        result = await self._session.execute(
            select(SupportTicket)
            .where(SupportTicket.order_id == order_id)
            .order_by(SupportTicket.opened_at.desc())
        )
        return result.scalars().first()

    async def list_for_user(self, user_id: int, limit: int = 12, offset: int = 0) -> list[SupportTicket]:
        result = await self._session.execute(
            select(SupportTicket).where(SupportTicket.user_id == user_id).order_by(SupportTicket.opened_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_user(self, user_id: int) -> int:
        from sqlalchemy import func
        result = await self._session.execute(
            select(func.count(SupportTicket.id)).where(SupportTicket.user_id == user_id)
        )
        return result.scalar() or 0

    async def list_open(self, limit: int = 20) -> list[SupportTicket]:
        result = await self._session.execute(
            select(SupportTicket)
            .where(SupportTicket.status.in_([TicketStatus.OPEN, TicketStatus.PENDING]))
            .order_by(SupportTicket.opened_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def add_message(self, **fields: object) -> TicketMessage:
        msg = TicketMessage(**fields)
        self._session.add(msg)
        await self._session.flush()
        return msg

    async def list_messages(self, ticket_id: int, limit: int = 50) -> list[TicketMessage]:
        result = await self._session.execute(
            select(TicketMessage).where(TicketMessage.ticket_id == ticket_id).order_by(TicketMessage.created_at).limit(limit)
        )
        return list(result.scalars().all())
