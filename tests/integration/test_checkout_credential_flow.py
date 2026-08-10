"""The whole purchase, end to end, at credential level.

Covers the flow in requirement 14: two buyers on the same product at the same time, one pays, one
walks away, and neither ever sees the other's credential.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.bot.handlers.orders.checkout import on_checkout_cancel, render_checkout_confirm
from app.bot.callbacks import OrderCB
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
from app.database.repositories.wallet_repo import WalletRepo
from app.services import order_service, stock_hold_service, wallet_service
from app.database.models.wallet import TxnType


class _FakeMessage:
    async def edit_text(self, text: str, reply_markup=None):  # noqa: ANN001 - test double
        self.text = text
        return self


class _FakeQuery:
    def __init__(self) -> None:
        self.message = _FakeMessage()

    async def answer(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003 - test double
        return None


async def _funded_user(sessionmaker, telegram_id: int, *, cents: int = 10_000) -> int:
    async with sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
            chat_id=telegram_id, default_locale="en",
        )
        await session.flush()
        await WalletRepo(session).get_or_create(user.id, currency="USD")
        await wallet_service.credit(
            session, user_id=user.id, amount_minor=cents, currency="USD",
            type_=TxnType.TOPUP, idempotency_key=f"seed:{telegram_id}",
        )
        await session.commit()
        return user.id


async def _product(sessionmaker, *, credentials: int) -> int:
    async with sessionmaker() as session:
        product = Product(
            category_id=None, name="Product A", slug="product-a", price_minor=500, currency="USD",
            status=ProductStatus.IN_STOCK, fulfillment_mode=FulfillmentMode.AUTO,
            warranty_days=0, is_active=True,
        )
        session.add(product)
        await session.flush()
        cipher = get_cipher()
        for n in range(credentials):
            session.add(
                StockItem(
                    product_id=product.id, payload=cipher.encrypt(f"CRED-{n + 1}"),
                    status=StockStatus.AVAILABLE,
                )
            )
        await session.commit()
        return product.id


def _user_obj(user_id: int):
    return SimpleNamespace(id=user_id, locale="en")


@pytest.mark.asyncio
async def test_the_buyer_is_delivered_the_exact_credential_they_held(sqlite_sessionmaker):
    """Not "a" credential — *theirs*. Anything else means the hold guaranteed nothing."""
    product_id = await _product(sqlite_sessionmaker, credentials=20)
    buyer = await _funded_user(sqlite_sessionmaker, 8001)

    async with sqlite_sessionmaker() as session:
        await render_checkout_confirm(session, product_id, _user_obj(buyer))
        await session.commit()

    async with sqlite_sessionmaker() as session:
        held = await stock_hold_service.get_hold(session, product_id, buyer)
        held_id, expected = held.id, get_cipher().decrypt(held.payload)

    async with sqlite_sessionmaker() as session:
        placed = await order_service.place_order(session, user_id=buyer, product_id=product_id)
        await session.commit()
        assert placed.delivered_payload == expected

    async with sqlite_sessionmaker() as session:
        row = await session.get(StockItem, held_id)
        assert row.status is StockStatus.DELIVERED
        assert row.held_by_user_id is None


@pytest.mark.asyncio
async def test_two_buyers_check_out_at_once_and_get_different_credentials(sqlite_sessionmaker):
    product_id = await _product(sqlite_sessionmaker, credentials=20)
    first = await _funded_user(sqlite_sessionmaker, 8002)
    second = await _funded_user(sqlite_sessionmaker, 8003)

    async with sqlite_sessionmaker() as session:
        await render_checkout_confirm(session, product_id, _user_obj(first))
        await render_checkout_confirm(session, product_id, _user_obj(second))
        await session.commit()

    async with sqlite_sessionmaker() as session:
        a = await stock_hold_service.get_hold(session, product_id, first)
        b = await stock_hold_service.get_hold(session, product_id, second)
        assert a.id != b.id, "neither buyer may be promised the other's login"
        assert await ProductRepo(session).available_stock_count(product_id) == 18

    async with sqlite_sessionmaker() as session:
        paid = await order_service.place_order(session, user_id=first, product_id=product_id)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        # The payer's credential is gone; the other buyer's hold is untouched.
        assert (await session.get(StockItem, a.id)).status is StockStatus.DELIVERED
        assert (await session.get(StockItem, b.id)).status is StockStatus.HELD
        assert paid.delivered_payload == get_cipher().decrypt(a.payload)


@pytest.mark.asyncio
async def test_backing_out_frees_the_credential_at_once(sqlite_sessionmaker):
    product_id = await _product(sqlite_sessionmaker, credentials=1)
    quitter = await _funded_user(sqlite_sessionmaker, 8004)

    async with sqlite_sessionmaker() as session:
        await render_checkout_confirm(session, product_id, _user_obj(quitter))
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await ProductRepo(session).available_stock_count(product_id) == 0

    async with sqlite_sessionmaker() as session:
        await on_checkout_cancel(
            _FakeQuery(), OrderCB(action="cancel", product_id=str(product_id)), session,
            _user_obj(quitter),
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await ProductRepo(session).available_stock_count(product_id) == 1
        assert await stock_hold_service.held_count(session, product_id) == 0


@pytest.mark.asyncio
async def test_the_last_credential_cannot_be_double_sold(sqlite_sessionmaker):
    """One credential, two buyers, both reach checkout. Only one can walk away with it."""
    product_id = await _product(sqlite_sessionmaker, credentials=1)
    winner = await _funded_user(sqlite_sessionmaker, 8005)
    loser = await _funded_user(sqlite_sessionmaker, 8006)

    async with sqlite_sessionmaker() as session:
        assert await render_checkout_confirm(session, product_id, _user_obj(winner)) is not None
        await session.commit()

    async with sqlite_sessionmaker() as session:
        # Nothing left to promise the second buyer, so the confirm screen refuses to render.
        assert await render_checkout_confirm(session, product_id, _user_obj(loser)) is None
        await session.commit()

    async with sqlite_sessionmaker() as session:
        await order_service.place_order(session, user_id=winner, product_id=product_id)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        with pytest.raises(Exception):
            await order_service.place_order(session, user_id=loser, product_id=product_id)

    async with sqlite_sessionmaker() as session:
        rows = (await session.execute(select(StockItem))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status is StockStatus.DELIVERED


@pytest.mark.asyncio
async def test_a_lapsed_hold_does_not_block_the_buyer_if_stock_is_free(sqlite_sessionmaker):
    """Their window closed but a credential is still around: they get one rather than an error."""
    product_id = await _product(sqlite_sessionmaker, credentials=2)
    buyer = await _funded_user(sqlite_sessionmaker, 8007)

    async with sqlite_sessionmaker() as session:
        await stock_hold_service.hold_one(session, product_id, buyer)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        rows = (
            await session.execute(select(StockItem).where(StockItem.status == StockStatus.HELD))
        ).scalars().all()
        for row in rows:
            row.held_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        placed = await order_service.place_order(session, user_id=buyer, product_id=product_id)
        await session.commit()
        assert placed.delivered_payload is not None
