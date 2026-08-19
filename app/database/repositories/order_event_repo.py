from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import new_event_number
from app.database.models.order_event import OrderEvent, OrderEventActor, OrderEventKind


class OrderEventRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        order_id: str,
        kind: OrderEventKind,
        actor: OrderEventActor = OrderEventActor.SYSTEM,
        actor_telegram_id: int | None = None,
        amount_minor: int | None = None,
        currency: str | None = None,
        reason: str | None = None,
        reference: str | None = None,
        at: datetime | None = None,
    ) -> OrderEvent:
        """Write one history line and hand back its minted ID.

        The ID is generated here rather than by the caller so that no code path can append an event
        without one — the ID is the only handle the admin search has on the thing that happened.
        """
        event = OrderEvent(
            event_number=new_event_number(kind.value),
            order_id=order_id,
            kind=kind,
            actor=actor,
            actor_telegram_id=actor_telegram_id,
            amount_minor=amount_minor,
            currency=currency,
            reason=reason,
            reference=reference,
            created_at=at or datetime.now(UTC),
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_for_order(self, order_id: str, *, limit: int = 50) -> list[OrderEvent]:
        """Oldest first — this renders as a timeline, and a timeline that starts at the end is a
        list. `id` breaks ties because several events are written inside one transaction and can
        share a timestamp to the microsecond."""
        result = await self._session.execute(
            select(OrderEvent)
            .where(OrderEvent.order_id == order_id)
            .order_by(OrderEvent.created_at, OrderEvent.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_by_event_number(self, event_number: str) -> OrderEvent | None:
        result = await self._session.execute(
            select(OrderEvent).where(func.upper(OrderEvent.event_number) == event_number.strip().upper())
        )
        return result.scalars().first()

    async def latest_of_kind(self, order_id: str, kind: OrderEventKind) -> OrderEvent | None:
        result = await self._session.execute(
            select(OrderEvent)
            .where(OrderEvent.order_id == order_id, OrderEvent.kind == kind)
            .order_by(OrderEvent.created_at.desc(), OrderEvent.id.desc())
            .limit(1)
        )
        return result.scalars().first()
