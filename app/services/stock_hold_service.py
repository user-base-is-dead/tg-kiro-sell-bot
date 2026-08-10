"""Reservations at the level of ONE credential, never the whole product.

The rule this file exists to enforce: **one credential belongs to at most one customer at a time**,
and one customer's reservation must not make the product look unavailable to anybody else while
another credential is still free.

The reservation lives on the `stock_items` row itself (`status`, `held_by_user_id`, `held_at`,
`held_until`). One row, one source of truth. A separate holds table can disagree with the
credential's own status, and a credential that reads HELD in one place and AVAILABLE in another is
exactly how the same login gets handed to two people.

Concurrency is handled by making every transition a single conditional `UPDATE` whose `WHERE`
clause restates the precondition — `... WHERE id = :id AND status = 'AVAILABLE'`. The database
decides the winner; `rowcount` tells us whether we were it. Nothing here does "check, then act",
because between the check and the act another buyer fits.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.catalog import StockItem, StockStatus
from app.utils.time import as_utc

HOLD_MINUTES = 5


def _is_free(now: datetime):
    """A credential nobody has a live claim on: plainly AVAILABLE, or HELD past its window.

    Lapsed holds are treated as free everywhere rather than only after the sweep relabels them.
    Waiting for the job would leave a credential unbuyable for up to one interval after its holder
    walked away, and — worse — the two definitions could disagree.
    """
    return or_(
        StockItem.status == StockStatus.AVAILABLE,
        and_(StockItem.status == StockStatus.HELD, StockItem.held_until <= now),
    )


async def hold_one(session: AsyncSession, product_id: int, user_id: int) -> StockItem | None:
    """Reserve one specific available credential for `user_id`. Returns it, or None if none is free.

    Re-entrant: a buyer who already holds a credential for this product gets the *same* one back
    with a refreshed window, rather than accumulating reservations by tapping Buy twice.

    The claim is a conditional UPDATE, so with ten buyers racing on one credential exactly one
    UPDATE matches a row and the other nine get `rowcount == 0` and move to the next candidate.
    """
    existing = await get_hold(session, product_id, user_id)
    if existing is not None:
        now = datetime.now(UTC)
        existing.held_at = now
        existing.held_until = now + timedelta(minutes=HOLD_MINUTES)
        await session.flush()
        return existing

    # Candidates are re-read each pass: a row lost to another buyer must not be retried, and one
    # freed by an expiring hold in the meantime should be picked up.
    while True:
        now = datetime.now(UTC)
        candidate = (
            await session.execute(
                select(StockItem.id)
                .where(StockItem.product_id == product_id, _is_free(now))
                .order_by(StockItem.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if candidate is None:
            return None

        result = await session.execute(
            update(StockItem)
            .where(StockItem.id == candidate, _is_free(now))
            .values(
                status=StockStatus.HELD,
                held_by_user_id=user_id,
                held_at=now,
                held_until=now + timedelta(minutes=HOLD_MINUTES),
            )
        )
        if result.rowcount:
            await session.flush()
            return await session.get(StockItem, candidate)
        # Somebody else took it between the SELECT and the UPDATE. Try the next one.


async def release(session: AsyncSession, product_id: int, user_id: int) -> int:
    """Hand this buyer's held credential(s) for a product straight back. Returns how many.

    Used by Back/Cancel: an abandoned checkout must not keep the credential off the shelf for the
    rest of the five minutes.
    """
    result = await session.execute(
        update(StockItem)
        .where(
            StockItem.product_id == product_id,
            StockItem.held_by_user_id == user_id,
            StockItem.status == StockStatus.HELD,
        )
        .values(status=StockStatus.AVAILABLE, held_by_user_id=None, held_at=None, held_until=None)
    )
    await session.flush()
    return result.rowcount or 0


async def get_hold(session: AsyncSession, product_id: int, user_id: int) -> StockItem | None:
    """This buyer's live held credential for the product, if the window is still open."""
    now = datetime.now(UTC)
    result = await session.execute(
        select(StockItem)
        .where(
            StockItem.product_id == product_id,
            StockItem.held_by_user_id == user_id,
            StockItem.status == StockStatus.HELD,
            StockItem.held_until > now,
        )
        .order_by(StockItem.id)
        .limit(1)
    )
    return result.scalars().first()


async def claim_held(session: AsyncSession, stock_item_id: int, user_id: int) -> bool:
    """HELD → RESERVED for this buyer, atomically. False if the hold is no longer theirs.

    This is the payment-succeeds-versus-hold-expires race (a credential must never be SOLD to one
    buyer and AVAILABLE to another). The `WHERE` restates every precondition, so if the expiry
    sweep won, this returns False and the order fails cleanly instead of delivering a credential
    that somebody else may already be holding.
    """
    result = await session.execute(
        update(StockItem)
        .where(
            StockItem.id == stock_item_id,
            StockItem.held_by_user_id == user_id,
            StockItem.status == StockStatus.HELD,
        )
        .values(status=StockStatus.RESERVED, held_by_user_id=None, held_at=None, held_until=None)
    )
    await session.flush()
    return bool(result.rowcount)


async def seconds_remaining(session: AsyncSession, product_id: int, user_id: int) -> int:
    """Seconds left on this buyer's hold, 0 if they have none."""
    hold = await get_hold(session, product_id, user_id)
    if hold is None or hold.held_until is None:
        return 0
    remaining = (as_utc(hold.held_until) - datetime.now(UTC)).total_seconds()
    return max(0, int(remaining))


async def expire_due(session: AsyncSession) -> int:
    """HELD → AVAILABLE for every hold whose window has closed. Returns how many were freed.

    The backend is the source of truth for expiry, not a countdown in the buyer's chat: they can
    close Telegram, lose signal, or never come back, and the credential still has to return to the
    shelf. Runs on a timer (see `app/jobs/scheduler.py`), and the read paths additionally filter on
    `held_until > now` so a credential is never treated as held past its window even in the seconds
    before the sweep catches it.
    """
    result = await session.execute(
        update(StockItem)
        .where(StockItem.status == StockStatus.HELD, StockItem.held_until <= datetime.now(UTC))
        .values(status=StockStatus.AVAILABLE, held_by_user_id=None, held_at=None, held_until=None)
    )
    await session.flush()
    return result.rowcount or 0


async def count_by_status(session: AsyncSession, product_id: int, status: StockStatus) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(StockItem)
        .where(StockItem.product_id == product_id, StockItem.status == status)
    )
    return int(result.scalar_one())


async def held_count(session: AsyncSession, product_id: int) -> int:
    """Credentials currently held by *anyone*, counting only windows that are still open.

    Deliberately not `status == HELD` alone: between a hold expiring and the sweep running, the row
    still says HELD but the credential is effectively free, and reporting it as held would tell a
    waiting buyer to keep waiting for something already available.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        select(func.count())
        .select_from(StockItem)
        .where(
            StockItem.product_id == product_id,
            StockItem.status == StockStatus.HELD,
            StockItem.held_until > now,
        )
    )
    return int(result.scalar_one())
