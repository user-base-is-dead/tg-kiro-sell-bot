from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.order import Order
from app.database.models.order_event import OrderEvent, OrderEventActor, OrderEventKind
from app.database.repositories.order_event_repo import OrderEventRepo


async def record(
    session: AsyncSession,
    order: Order,
    kind: OrderEventKind,
    *,
    actor: OrderEventActor = OrderEventActor.SYSTEM,
    actor_telegram_id: int | None = None,
    amount_minor: int | None = None,
    reason: str | None = None,
    reference: str | None = None,
    at: datetime | None = None,
) -> OrderEvent:
    """Append one line to an order's history and return it, ID and all.

    A thin wrapper over the repo that exists for one reason: the currency comes off the order rather
    than from the caller. An amount recorded in the wrong currency reads as a plausible number and is
    the kind of mistake nobody notices until somebody is refunded the wrong sum.
    """
    return await OrderEventRepo(session).append(
        order_id=order.id,
        kind=kind,
        actor=actor,
        actor_telegram_id=actor_telegram_id,
        amount_minor=amount_minor,
        currency=order.currency if amount_minor is not None else None,
        reason=reason,
        reference=reference,
        at=at,
    )


async def timeline(session: AsyncSession, order_id: str, *, limit: int = 50) -> list[OrderEvent]:
    return await OrderEventRepo(session).list_for_order(order_id, limit=limit)
