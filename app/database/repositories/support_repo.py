from __future__ import annotations

from sqlalchemy import select
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
        return result.scalar_one_or_none()

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
