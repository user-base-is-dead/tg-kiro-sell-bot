"""SQLite hands stored timestamps back **naive**, and comparing one to an aware `now` raises
`TypeError: can't subtract offset-naive and offset-aware datetimes`.

This bug has now shipped four separate times, each time as an unexplained "Something went wrong on
our end" on a different screen, because it hides behind the identity map: the object you just
created is still the aware Python value, so it only blows up once the row is genuinely re-read from
the database. `app/utils/time.as_utc` is the fix; these tests re-read every affected path from a
fresh session, which is the only way the naive value actually appears.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models.order import Order, OrderItem, OrderStatus, Warranty, WarrantyStatus
from app.services.warranty_service import display_remaining
from app.database.repositories.interaction_state_repo import InteractionStateRepo
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.warranty_repo import WarrantyRepo
from app.services.gift_service import create_gift_code, get_active_gift


async def _user(sessionmaker, telegram_id: int) -> int:
    async with sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
            chat_id=telegram_id, default_locale="en",
        )
        await session.commit()
        return user.id


async def _warranty(sessionmaker, user_id: int, *, days: int) -> int:
    async with sessionmaker() as session:
        order = Order(
            order_number="ORD-TEST01", user_id=user_id, status=OrderStatus.COMPLETED,
            subtotal_minor=0, discount_minor=0, total_minor=0, currency="USD",
            idempotency_key=f"t:{user_id}:{days}", placed_at=datetime.now(UTC),
        )
        session.add(order)
        await session.flush()
        item = OrderItem(
            order_id=order.id, product_id=None, product_name="Kiro Pro",
            unit_price_minor=0, qty=1, warranty_days=days,
        )
        session.add(item)
        await session.flush()
        now = datetime.now(UTC)
        warranty = Warranty(
            order_item_id=item.id, user_id=user_id, starts_at=now,
            expires_at=now + timedelta(days=days), status=WarrantyStatus.ACTIVE,
        )
        session.add(warranty)
        await session.commit()
        return warranty.id


@pytest.mark.asyncio
async def test_the_warranty_screen_renders_a_stored_warranty(sqlite_sessionmaker):
    """The reported crash: tapping 🔧 Warranty died for anyone who actually had one."""
    user_id = await _user(sqlite_sessionmaker, 9001)
    warranty_id = await _warranty(sqlite_sessionmaker, user_id, days=30)

    async with sqlite_sessionmaker() as session:
        stored = await WarrantyRepo(session).get_by_id(warranty_id)
        remaining = display_remaining(stored)  # must not raise

    assert remaining.endswith("h"), remaining
    assert remaining != "expired"


@pytest.mark.asyncio
async def test_an_elapsed_warranty_reads_as_expired(sqlite_sessionmaker):
    user_id = await _user(sqlite_sessionmaker, 9002)
    warranty_id = await _warranty(sqlite_sessionmaker, user_id, days=30)

    async with sqlite_sessionmaker() as session:
        stored = await WarrantyRepo(session).get_by_id(warranty_id)
        stored.expires_at = datetime.now(UTC) - timedelta(days=1)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        stored = await WarrantyRepo(session).get_by_id(warranty_id)
        assert display_remaining(stored) == "expired"


@pytest.mark.asyncio
async def test_a_stored_gift_code_expiry_can_be_compared(sqlite_sessionmaker):
    async with sqlite_sessionmaker() as session:
        await create_gift_code(
            session, value_minor=500, currency="USD", max_uses=1, per_user_limit=1,
            expires_at=datetime.now(UTC) + timedelta(days=1), admin_id=999,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await get_active_gift(session) is not None  # must not raise


@pytest.mark.asyncio
async def test_an_expired_gift_code_is_skipped_not_crashed_on(sqlite_sessionmaker):
    async with sqlite_sessionmaker() as session:
        await create_gift_code(
            session, value_minor=500, currency="USD", max_uses=1, per_user_limit=1,
            expires_at=datetime.now(UTC) - timedelta(days=1), admin_id=999,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await get_active_gift(session) is None


@pytest.mark.asyncio
async def test_a_stored_interaction_state_can_be_read_back(sqlite_sessionmaker):
    user_id = await _user(sqlite_sessionmaker, 9003)

    async with sqlite_sessionmaker() as session:
        token = await InteractionStateRepo(session).create(user_id, {"hello": "world"})
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await InteractionStateRepo(session).get(token) == {"hello": "world"}
