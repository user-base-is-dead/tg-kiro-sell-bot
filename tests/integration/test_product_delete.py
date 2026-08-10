"""Deleting a product must actually delete it, and must not cost a buyer their order history.

The admin screen used to refuse with "this product has order history — disable it instead", because
`order_items.product_id` was a NOT NULL foreign key. Migration 0013 made that column (and
`stock_items.product_id`) nullable so `ProductRepo.delete` can detach instead of being blocked.
These tests pin both halves: the product is really gone, and everything the buyer can see survives.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.security import get_cipher
from app.database.models.catalog import (
    Category,
    FulfillmentMode,
    Product,
    ProductStatus,
    StockItem,
    StockStatus,
)
from app.database.models.order import OrderItem
from app.database.models.wallet import TxnType
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services import order_service, stock_hold_service, wallet_service


async def _make_user(sessionmaker, telegram_id: int) -> int:
    async with sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
            chat_id=telegram_id, default_locale="en",
        )
        await session.flush()
        await WalletRepo(session).get_or_create(user.id, currency="USD")
        await wallet_service.credit(
            session, user_id=user.id, amount_minor=10_000, currency="USD",
            type_=TxnType.TOPUP, idempotency_key=f"seed:{telegram_id}",
        )
        await session.commit()
        return user.id


async def _buy(sessionmaker, user_id: int, product_id: int) -> None:
    """A real purchase, which is what puts a row in `order_items` pointing at the product."""
    async with sessionmaker() as session:
        await order_service.place_order(session, user_id=user_id, product_id=product_id)
        await session.commit()


async def _make_product(sessionmaker, *, stock: int = 1, payload: str = "SECRET-KEY-123") -> int:
    async with sessionmaker() as session:
        category = Category(name="Cat", slug="cat", sort_order=1)
        session.add(category)
        await session.flush()

        product = Product(
            category_id=category.id,
            name="Netflix 1 Month",
            slug="netflix-1m",
            price_minor=999,
            currency="USD",
            status=ProductStatus.IN_STOCK,
            fulfillment_mode=FulfillmentMode.AUTO,
            warranty_days=30,
            is_active=True,
        )
        session.add(product)
        await session.flush()

        cipher = get_cipher()
        for _ in range(stock):
            session.add(
                StockItem(
                    product_id=product.id,
                    payload=cipher.encrypt(payload),
                    status=StockStatus.AVAILABLE,
                )
            )
        await session.commit()
        return product.id


async def _delete(sessionmaker, product_id: int) -> None:
    async with sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        await ProductRepo(session).delete(product)
        await session.commit()


@pytest.mark.asyncio
async def test_a_product_with_order_history_is_really_deleted(sqlite_sessionmaker):
    """The exact case the old code refused: the product has been bought."""
    product_id = await _make_product(sqlite_sessionmaker)
    user_id = await _make_user(sqlite_sessionmaker, 4001)

    await _buy(sqlite_sessionmaker, user_id, product_id)

    await _delete(sqlite_sessionmaker, product_id)

    async with sqlite_sessionmaker() as session:
        assert await ProductRepo(session).get_by_id(product_id) is None, "the product must be gone"


@pytest.mark.asyncio
async def test_the_buyers_order_history_survives_the_delete(sqlite_sessionmaker):
    """`order_items` keeps its own name/price snapshot, so the row still renders with no product."""
    product_id = await _make_product(sqlite_sessionmaker)
    user_id = await _make_user(sqlite_sessionmaker, 4002)

    await _buy(sqlite_sessionmaker, user_id, product_id)

    await _delete(sqlite_sessionmaker, product_id)

    async with sqlite_sessionmaker() as session:
        items = (await session.execute(select(OrderItem))).scalars().all()
        assert len(items) == 1, "the order item must not be deleted with the product"
        assert items[0].product_id is None, "it is detached, not dangling"
        assert items[0].product_name == "Netflix 1 Month", "the name snapshot is what renders it"
        assert items[0].unit_price_minor == 999, "the price paid is snapshotted"
        assert items[0].warranty_days == 30, "the buyer keeps their warranty window"


@pytest.mark.asyncio
async def test_a_delivered_payload_survives_but_unsold_stock_is_discarded(sqlite_sessionmaker):
    """The buyer can still be shown what they bought under warranty. Unsold keys for a product
    nobody can buy any more are dead weight and go."""
    product_id = await _make_product(sqlite_sessionmaker, stock=3)
    user_id = await _make_user(sqlite_sessionmaker, 4003)

    await _buy(sqlite_sessionmaker, user_id, product_id)

    await _delete(sqlite_sessionmaker, product_id)

    async with sqlite_sessionmaker() as session:
        remaining = (await session.execute(select(StockItem))).scalars().all()
        assert len(remaining) == 1, "only the sold item survives; the 2 unsold ones are discarded"
        assert remaining[0].status is not StockStatus.AVAILABLE
        assert remaining[0].product_id is None
        assert get_cipher().decrypt(remaining[0].payload) == "SECRET-KEY-123"


@pytest.mark.asyncio
async def test_a_held_credential_does_not_block_the_delete(sqlite_sessionmaker):
    """A hold is a 5-minute reservation, not history — the credential goes with the product."""
    product_id = await _make_product(sqlite_sessionmaker)
    user_id = await _make_user(sqlite_sessionmaker, 4004)

    async with sqlite_sessionmaker() as session:
        assert await stock_hold_service.hold_one(session, product_id, user_id) is not None
        await session.commit()

    await _delete(sqlite_sessionmaker, product_id)

    async with sqlite_sessionmaker() as session:
        assert await ProductRepo(session).get_by_id(product_id) is None
        assert (await session.execute(select(StockItem))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_product_nobody_ever_bought_deletes_cleanly(sqlite_sessionmaker):
    product_id = await _make_product(sqlite_sessionmaker, stock=2)

    await _delete(sqlite_sessionmaker, product_id)

    async with sqlite_sessionmaker() as session:
        assert await ProductRepo(session).get_by_id(product_id) is None
        assert (await session.execute(select(StockItem))).scalars().all() == []
