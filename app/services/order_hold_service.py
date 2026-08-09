from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.order import OrderHold


async def create_hold(session: AsyncSession, product_id: int, user_id: int, order_id: str | None = None) -> OrderHold:
    """Create a 5-minute hold on a product for a user."""
    now = datetime.now(UTC)
    hold = OrderHold(
        product_id=product_id,
        user_id=user_id,
        order_id=order_id,
        held_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    session.add(hold)
    await session.flush()
    return hold


async def release_hold(session: AsyncSession, product_id: int, user_id: int) -> None:
    """Release hold for a specific user on a product."""
    await session.execute(
        delete(OrderHold).where(
            (OrderHold.product_id == product_id) & (OrderHold.user_id == user_id)
        )
    )
    await session.flush()


async def get_hold_for_product(session: AsyncSession, product_id: int, user_id: int) -> OrderHold | None:
    """Get active hold for a product by a user."""
    now = datetime.now(UTC)
    result = await session.execute(
        select(OrderHold).where(
            (OrderHold.product_id == product_id)
            & (OrderHold.user_id == user_id)
            & (OrderHold.expires_at > now)
        )
    )
    return result.scalar_one_or_none()


async def get_hold_on_product(session: AsyncSession, product_id: int) -> OrderHold | None:
    """Get any active hold on a product (for other users)."""
    now = datetime.now(UTC)
    result = await session.execute(
        select(OrderHold)
        .where((OrderHold.product_id == product_id) & (OrderHold.expires_at > now))
        .order_by(OrderHold.held_at.desc())
    )
    return result.scalar_one_or_none()


async def get_time_remaining(session: AsyncSession, product_id: int, user_id: int) -> int:
    """Get seconds remaining on hold (0 if no hold or expired)."""
    hold = await get_hold_for_product(session, product_id, user_id)
    if hold is None:
        return 0

    now = datetime.now(UTC)
    remaining = (hold.expires_at - now).total_seconds()
    return max(0, int(remaining))


async def clean_expired_holds(session: AsyncSession) -> int:
    """Clean up expired holds and return count deleted."""
    now = datetime.now(UTC)
    result = await session.execute(
        delete(OrderHold).where(OrderHold.expires_at <= now)
    )
    await session.flush()
    return result.rowcount or 0
