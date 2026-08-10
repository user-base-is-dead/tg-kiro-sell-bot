"""Reservations happen per credential, never per product.

The rule everything here defends: **one credential belongs to at most one customer at a time**, and
one customer's reservation must not make the product unavailable to anyone else while another
credential is still free. The previous implementation reserved the whole product, so one buyer
entering checkout made all 20 logins read as unavailable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.security import get_cipher
from app.database.models.catalog import (
    FulfillmentMode,
    Product,
    ProductStatus,
    StockItem,
    StockStatus,
)
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.user_repo import UserRepo
from app.services import stock_hold_service
from app.services.catalog_service import compute_display_status


async def _user(sessionmaker, telegram_id: int) -> int:
    async with sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
            chat_id=telegram_id, default_locale="en",
        )
        await session.commit()
        return user.id


async def _product(sessionmaker, *, credentials: int) -> int:
    async with sessionmaker() as session:
        product = Product(
            category_id=None,
            name="Product A",
            slug="product-a",
            price_minor=500,
            currency="USD",
            status=ProductStatus.IN_STOCK,
            fulfillment_mode=FulfillmentMode.AUTO,
            warranty_days=0,
            is_active=True,
            low_stock_threshold=2,
        )
        session.add(product)
        await session.flush()
        cipher = get_cipher()
        for n in range(credentials):
            session.add(
                StockItem(
                    product_id=product.id,
                    payload=cipher.encrypt(f"CRED-{n + 1}"),
                    status=StockStatus.AVAILABLE,
                )
            )
        await session.commit()
        return product.id


async def _available(sessionmaker, product_id: int) -> int:
    async with sessionmaker() as session:
        return await ProductRepo(session).available_stock_count(product_id)


# ---- 1. One buyer takes ONE credential, not the product ----


@pytest.mark.asyncio
async def test_a_hold_takes_exactly_one_credential(sqlite_sessionmaker):
    product_id = await _product(sqlite_sessionmaker, credentials=20)
    buyer = await _user(sqlite_sessionmaker, 7001)

    async with sqlite_sessionmaker() as session:
        held = await stock_hold_service.hold_one(session, product_id, buyer)
        await session.commit()
        assert held is not None

    assert await _available(sqlite_sessionmaker, product_id) == 19, "the other 19 stay on the shelf"

    async with sqlite_sessionmaker() as session:
        rows = (await session.execute(select(StockItem))).scalars().all()
        assert sum(r.status is StockStatus.HELD for r in rows) == 1
        assert sum(r.status is StockStatus.AVAILABLE for r in rows) == 19


@pytest.mark.asyncio
async def test_the_product_stays_in_stock_for_everyone_else(sqlite_sessionmaker):
    """The headline bug: one checkout used to make the whole product look unavailable."""
    product_id = await _product(sqlite_sessionmaker, credentials=20)
    buyer = await _user(sqlite_sessionmaker, 7002)

    async with sqlite_sessionmaker() as session:
        await stock_hold_service.hold_one(session, product_id, buyer)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        view = await compute_display_status(session, product)

    assert view.display_status is ProductStatus.IN_STOCK
    assert view.available_stock == 19


# ---- 7. Many buyers, many credentials, all independent ----


@pytest.mark.asyncio
async def test_ten_buyers_get_ten_different_credentials(sqlite_sessionmaker):
    product_id = await _product(sqlite_sessionmaker, credentials=20)
    buyers = [await _user(sqlite_sessionmaker, 7100 + n) for n in range(10)]

    held_ids = []
    async with sqlite_sessionmaker() as session:
        for buyer in buyers:
            held = await stock_hold_service.hold_one(session, product_id, buyer)
            assert held is not None
            held_ids.append(held.id)
        await session.commit()

    assert len(set(held_ids)) == 10, "no credential may be handed to two buyers"
    assert await _available(sqlite_sessionmaker, product_id) == 10


@pytest.mark.asyncio
async def test_tapping_buy_twice_does_not_take_a_second_credential(sqlite_sessionmaker):
    """Re-entrant: the buyer gets the same credential back with a refreshed window."""
    product_id = await _product(sqlite_sessionmaker, credentials=5)
    buyer = await _user(sqlite_sessionmaker, 7003)

    async with sqlite_sessionmaker() as session:
        first = await stock_hold_service.hold_one(session, product_id, buyer)
        second = await stock_hold_service.hold_one(session, product_id, buyer)
        third = await stock_hold_service.hold_one(session, product_id, buyer)
        await session.commit()

    assert first.id == second.id == third.id
    assert await _available(sqlite_sessionmaker, product_id) == 4


# ---- 8. Never assign one credential to two users ----


@pytest.mark.asyncio
async def test_the_21st_buyer_gets_nothing_rather_than_a_taken_credential(sqlite_sessionmaker):
    product_id = await _product(sqlite_sessionmaker, credentials=20)
    buyers = [await _user(sqlite_sessionmaker, 7200 + n) for n in range(21)]

    held_ids = []
    async with sqlite_sessionmaker() as session:
        for buyer in buyers[:20]:
            held_ids.append((await stock_hold_service.hold_one(session, product_id, buyer)).id)
        latecomer = await stock_hold_service.hold_one(session, product_id, buyers[20])
        await session.commit()

    assert len(set(held_ids)) == 20
    assert latecomer is None, "an unpaid hold is still a hold — it is not reassigned"


# ---- 10. Concurrency ----


@pytest.mark.asyncio
async def test_only_one_of_three_simultaneous_buyers_gets_the_last_credential(sqlite_sessionmaker):
    """Three buyers race for one credential in interleaved sessions. The conditional UPDATE decides
    the winner; the losers must come away with nothing rather than a duplicate."""
    product_id = await _product(sqlite_sessionmaker, credentials=1)
    buyers = [await _user(sqlite_sessionmaker, 7300 + n) for n in range(3)]

    results = []
    sessions = [sqlite_sessionmaker() for _ in range(3)]
    try:
        for session, buyer in zip(sessions, buyers, strict=True):
            held = await stock_hold_service.hold_one(session, product_id, buyer)
            if held is not None:
                await session.commit()
            results.append(held)
    finally:
        for session in sessions:
            await session.close()

    assert sum(r is not None for r in results) == 1, "exactly one winner"


# ---- 3. Payment succeeds ----


@pytest.mark.asyncio
async def test_paying_turns_the_held_credential_into_a_sale(sqlite_sessionmaker):
    product_id = await _product(sqlite_sessionmaker, credentials=20)
    buyer = await _user(sqlite_sessionmaker, 7004)

    async with sqlite_sessionmaker() as session:
        held = await stock_hold_service.hold_one(session, product_id, buyer)
        held_id = held.id
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await stock_hold_service.claim_held(session, held_id, buyer) is True
        await session.commit()

    async with sqlite_sessionmaker() as session:
        row = await session.get(StockItem, held_id)
        assert row.status is StockStatus.RESERVED, "it belongs to the buyer now"
        assert row.held_by_user_id is None
        assert await stock_hold_service.held_count(session, product_id) == 0

    assert await _available(sqlite_sessionmaker, product_id) == 19, "available does not rebound"


@pytest.mark.asyncio
async def test_a_credential_cannot_be_claimed_by_a_different_buyer(sqlite_sessionmaker):
    product_id = await _product(sqlite_sessionmaker, credentials=1)
    holder = await _user(sqlite_sessionmaker, 7005)
    thief = await _user(sqlite_sessionmaker, 7006)

    async with sqlite_sessionmaker() as session:
        held_id = (await stock_hold_service.hold_one(session, product_id, holder)).id
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await stock_hold_service.claim_held(session, held_id, thief) is False


# ---- 4. Expiry ----


async def _expire_now(sessionmaker, product_id: int) -> None:
    """Wind the window back so the hold has lapsed, without sleeping for five minutes."""
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(StockItem).where(
                    StockItem.product_id == product_id, StockItem.status == StockStatus.HELD
                )
            )
        ).scalars().all()
        past = datetime.now(UTC) - timedelta(seconds=1)
        for row in rows:
            row.held_until = past
        await session.commit()


@pytest.mark.asyncio
async def test_an_expired_hold_returns_the_same_credential_to_the_pool(sqlite_sessionmaker):
    product_id = await _product(sqlite_sessionmaker, credentials=20)
    buyer = await _user(sqlite_sessionmaker, 7007)

    async with sqlite_sessionmaker() as session:
        held_id = (await stock_hold_service.hold_one(session, product_id, buyer)).id
        await session.commit()

    await _expire_now(sqlite_sessionmaker, product_id)

    async with sqlite_sessionmaker() as session:
        assert await stock_hold_service.expire_due(session) == 1
        await session.commit()

    async with sqlite_sessionmaker() as session:
        row = await session.get(StockItem, held_id)
        assert row.status is StockStatus.AVAILABLE
        assert row.held_by_user_id is None

    assert await _available(sqlite_sessionmaker, product_id) == 20


@pytest.mark.asyncio
async def test_a_lapsed_hold_is_available_before_the_sweep_runs(sqlite_sessionmaker):
    """Availability must not wait on the job. Otherwise the product is unbuyable for up to one
    interval after the holder walked away."""
    product_id = await _product(sqlite_sessionmaker, credentials=1)
    buyer = await _user(sqlite_sessionmaker, 7008)

    async with sqlite_sessionmaker() as session:
        await stock_hold_service.hold_one(session, product_id, buyer)
        await session.commit()

    await _expire_now(sqlite_sessionmaker, product_id)

    assert await _available(sqlite_sessionmaker, product_id) == 1

    async with sqlite_sessionmaker() as session:
        assert await stock_hold_service.held_count(session, product_id) == 0
        other = await _user(sqlite_sessionmaker, 7009)
        assert await stock_hold_service.hold_one(session, product_id, other) is not None


@pytest.mark.asyncio
async def test_an_expired_hold_cannot_still_be_claimed(sqlite_sessionmaker):
    """Requirement 12: a credential must never end up sold to A and available to B."""
    product_id = await _product(sqlite_sessionmaker, credentials=1)
    slow = await _user(sqlite_sessionmaker, 7010)
    fast = await _user(sqlite_sessionmaker, 7011)

    async with sqlite_sessionmaker() as session:
        held_id = (await stock_hold_service.hold_one(session, product_id, slow)).id
        await session.commit()

    await _expire_now(sqlite_sessionmaker, product_id)
    async with sqlite_sessionmaker() as session:
        await stock_hold_service.expire_due(session)
        await session.commit()

    # The credential is now somebody else's.
    async with sqlite_sessionmaker() as session:
        await stock_hold_service.hold_one(session, product_id, fast)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await stock_hold_service.claim_held(session, held_id, slow) is False, (
            "the lapsed holder must not be able to take a credential someone else now holds"
        )


# ---- 5. Cancel releases immediately ----


@pytest.mark.asyncio
async def test_cancelling_returns_the_credential_without_waiting(sqlite_sessionmaker):
    product_id = await _product(sqlite_sessionmaker, credentials=1)
    buyer = await _user(sqlite_sessionmaker, 7012)

    async with sqlite_sessionmaker() as session:
        await stock_hold_service.hold_one(session, product_id, buyer)
        await session.commit()

    assert await _available(sqlite_sessionmaker, product_id) == 0

    async with sqlite_sessionmaker() as session:
        assert await stock_hold_service.release(session, product_id, buyer) == 1
        await session.commit()

    assert await _available(sqlite_sessionmaker, product_id) == 1


@pytest.mark.asyncio
async def test_cancelling_touches_nobody_elses_hold(sqlite_sessionmaker):
    product_id = await _product(sqlite_sessionmaker, credentials=5)
    quitter = await _user(sqlite_sessionmaker, 7013)
    stayer = await _user(sqlite_sessionmaker, 7014)

    async with sqlite_sessionmaker() as session:
        await stock_hold_service.hold_one(session, product_id, quitter)
        stayers_id = (await stock_hold_service.hold_one(session, product_id, stayer)).id
        await session.commit()

    async with sqlite_sessionmaker() as session:
        await stock_hold_service.release(session, product_id, quitter)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert (await session.get(StockItem, stayers_id)).status is StockStatus.HELD
        assert await stock_hold_service.held_count(session, product_id) == 1


# ---- 6 & 13. What the next shopper sees ----


@pytest.mark.asyncio
async def test_the_last_credential_held_reads_as_temporarily_unavailable(sqlite_sessionmaker):
    """Not OUT_OF_STOCK: this state un-does itself, so the shopper is told to wait."""
    product_id = await _product(sqlite_sessionmaker, credentials=1)
    buyer = await _user(sqlite_sessionmaker, 7015)

    async with sqlite_sessionmaker() as session:
        await stock_hold_service.hold_one(session, product_id, buyer)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        view = await compute_display_status(session, product)

    assert view.display_status is ProductStatus.ON_HOLD
    assert view.available_stock == 0


@pytest.mark.asyncio
async def test_it_becomes_out_of_stock_once_that_hold_is_paid(sqlite_sessionmaker):
    product_id = await _product(sqlite_sessionmaker, credentials=1)
    buyer = await _user(sqlite_sessionmaker, 7016)

    async with sqlite_sessionmaker() as session:
        held_id = (await stock_hold_service.hold_one(session, product_id, buyer)).id
        await session.commit()
    async with sqlite_sessionmaker() as session:
        await stock_hold_service.claim_held(session, held_id, buyer)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        view = await compute_display_status(session, product)

    assert view.display_status is ProductStatus.OUT_OF_STOCK


@pytest.mark.asyncio
async def test_it_becomes_buyable_again_once_that_hold_lapses(sqlite_sessionmaker):
    product_id = await _product(sqlite_sessionmaker, credentials=1)
    buyer = await _user(sqlite_sessionmaker, 7017)

    async with sqlite_sessionmaker() as session:
        await stock_hold_service.hold_one(session, product_id, buyer)
        await session.commit()

    await _expire_now(sqlite_sessionmaker, product_id)

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        view = await compute_display_status(session, product)

    assert view.display_status is not ProductStatus.ON_HOLD
    assert view.available_stock == 1
