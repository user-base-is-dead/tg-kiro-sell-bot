"""A gift code carries its own items. It never touches the product catalog.

This replaces the old PRODUCT gift, which handed out a catalog product as a free order and consumed
a real `stock_items` row. A giveaway is not a sale: that let a promo quietly empty the shelf paying
customers were queueing for, and gift stock could not be counted or topped up on its own.
"""

from __future__ import annotations

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
from app.database.models.gift import GiftItem, GiftItemStatus, GiftKind, GiftStatus
from app.database.models.order import Order, Warranty
from app.database.repositories.gift_repo import GiftRepo
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services.gift_service import (
    available_item_count,
    create_gift_code,
    redeem_gift_code,
)
from app.utils.errors import UserError


async def _user(sessionmaker, telegram_id: int) -> int:
    async with sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
            chat_id=telegram_id, default_locale="en",
        )
        await session.commit()
        return user.id


async def _code(sessionmaker, items: list[str], *, per_user_limit: int = 1) -> str:
    async with sessionmaker() as session:
        code = await create_gift_code(
            session, item_payloads=items, currency="USD", max_uses=0,
            per_user_limit=per_user_limit, expires_at=None, admin_id=999,
        )
        await session.commit()
        return code


# ---- The items belong to the code ----


@pytest.mark.asyncio
async def test_the_items_are_stored_encrypted_against_the_code(sqlite_sessionmaker):
    await _code(sqlite_sessionmaker, ["KEY-AAA", "KEY-BBB", "KEY-CCC"])

    async with sqlite_sessionmaker() as session:
        rows = (await session.execute(select(GiftItem))).scalars().all()
        assert len(rows) == 3
        assert all(r.status is GiftItemStatus.AVAILABLE for r in rows)
        stored = {get_cipher().decrypt(r.payload) for r in rows}
        assert stored == {"KEY-AAA", "KEY-BBB", "KEY-CCC"}
        assert all(r.payload != "KEY-AAA" for r in rows), "payloads are never stored in plaintext"


@pytest.mark.asyncio
async def test_redemptions_are_capped_at_the_item_count(sqlite_sessionmaker):
    """`max_uses` is not asked for on this branch — any other number is a promise the code cannot
    keep. Too high and a claimer burns their redemption on nothing; too low and items are stranded."""
    await _code(sqlite_sessionmaker, ["A", "B", "C"])

    async with sqlite_sessionmaker() as session:
        gift = (await GiftRepo(session).list_all())[0]
        assert gift.kind is GiftKind.ITEM
        assert gift.max_uses == 3


@pytest.mark.asyncio
async def test_a_code_must_grant_exactly_one_thing(sqlite_sessionmaker):
    async with sqlite_sessionmaker() as session:
        with pytest.raises(ValueError):
            await create_gift_code(
                session, value_minor=500, item_payloads=["A"], currency="USD", max_uses=1,
                per_user_limit=1, expires_at=None, admin_id=999,
            )
        with pytest.raises(ValueError):
            await create_gift_code(
                session, currency="USD", max_uses=1, per_user_limit=1, expires_at=None, admin_id=999,
            )


@pytest.mark.asyncio
async def test_an_empty_item_list_is_refused(sqlite_sessionmaker):
    async with sqlite_sessionmaker() as session:
        with pytest.raises(ValueError):
            await create_gift_code(
                session, item_payloads=["   ", ""], currency="USD", max_uses=0,
                per_user_limit=1, expires_at=None, admin_id=999,
            )


# ---- Claiming ----


@pytest.mark.asyncio
async def test_a_claim_hands_over_one_item(sqlite_sessionmaker):
    code = await _code(sqlite_sessionmaker, ["KEY-AAA", "KEY-BBB"])
    user_id = await _user(sqlite_sessionmaker, 9101)

    async with sqlite_sessionmaker() as session:
        claimed = await redeem_gift_code(session, user_id=user_id, code_plaintext=code)
        await session.commit()

    assert claimed.delivered_payload in {"KEY-AAA", "KEY-BBB"}

    async with sqlite_sessionmaker() as session:
        assert await available_item_count(session, claimed.gift.id) == 1
        taken = (
            await session.execute(
                select(GiftItem).where(GiftItem.status == GiftItemStatus.DELIVERED)
            )
        ).scalars().all()
        assert len(taken) == 1
        assert taken[0].claimed_by_user_id == user_id


@pytest.mark.asyncio
async def test_two_claimers_never_get_the_same_item(sqlite_sessionmaker):
    code = await _code(sqlite_sessionmaker, ["KEY-AAA", "KEY-BBB"])
    first = await _user(sqlite_sessionmaker, 9102)
    second = await _user(sqlite_sessionmaker, 9103)

    payloads = []
    for user_id in (first, second):
        async with sqlite_sessionmaker() as session:
            claimed = await redeem_gift_code(session, user_id=user_id, code_plaintext=code)
            await session.commit()
            payloads.append(claimed.delivered_payload)

    assert set(payloads) == {"KEY-AAA", "KEY-BBB"}


@pytest.mark.asyncio
async def test_the_code_is_exhausted_once_the_items_run_out(sqlite_sessionmaker):
    code = await _code(sqlite_sessionmaker, ["ONLY-ONE"])
    first = await _user(sqlite_sessionmaker, 9104)
    latecomer = await _user(sqlite_sessionmaker, 9105)

    async with sqlite_sessionmaker() as session:
        await redeem_gift_code(session, user_id=first, code_plaintext=code)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        with pytest.raises(UserError):
            await redeem_gift_code(session, user_id=latecomer, code_plaintext=code)

    async with sqlite_sessionmaker() as session:
        assert (await GiftRepo(session).list_all())[0].status is GiftStatus.EXHAUSTED


@pytest.mark.asyncio
async def test_a_claim_creates_no_order_and_no_warranty(sqlite_sessionmaker):
    """A giveaway is not a purchase. `gift_items.claimed_by_user_id` is the record of who got what.

    The warranty half matters on its own: a warranty is something a purchase buys, and a free item
    must never become a claim on a replacement. There is no code path from a gift to `Warranty` —
    this fails the moment someone adds one.
    """
    code = await _code(sqlite_sessionmaker, ["KEY-AAA"])
    user_id = await _user(sqlite_sessionmaker, 9106)

    async with sqlite_sessionmaker() as session:
        await redeem_gift_code(session, user_id=user_id, code_plaintext=code)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert (await session.execute(select(Order))).scalars().all() == []
        assert (await session.execute(select(Warranty))).scalars().all() == []


# ---- The catalog is never touched ----


@pytest.mark.asyncio
async def test_claiming_a_gift_does_not_consume_catalog_stock(sqlite_sessionmaker):
    """The reason this feature was rebuilt: a promo must not empty the shelf paying customers are
    queueing for."""
    async with sqlite_sessionmaker() as session:
        product = Product(
            category_id=None, name="Kiro Pro", slug="kiro-pro", price_minor=500, currency="USD",
            status=ProductStatus.IN_STOCK, fulfillment_mode=FulfillmentMode.AUTO,
            warranty_days=0, is_active=True,
        )
        session.add(product)
        await session.flush()
        for n in range(5):
            session.add(
                StockItem(
                    product_id=product.id, payload=get_cipher().encrypt(f"CRED-{n}"),
                    status=StockStatus.AVAILABLE,
                )
            )
        await session.commit()
        product_id = product.id

    code = await _code(sqlite_sessionmaker, ["GIFT-1", "GIFT-2"])
    user_id = await _user(sqlite_sessionmaker, 9107)

    async with sqlite_sessionmaker() as session:
        claimed = await redeem_gift_code(session, user_id=user_id, code_plaintext=code)
        await session.commit()

    assert claimed.delivered_payload.startswith("GIFT-"), "the gift's own item, not a catalog one"

    async with sqlite_sessionmaker() as session:
        assert await ProductRepo(session).available_stock_count(product_id) == 5


@pytest.mark.asyncio
async def test_a_gift_claim_moves_no_money(sqlite_sessionmaker):
    code = await _code(sqlite_sessionmaker, ["KEY-AAA"])
    user_id = await _user(sqlite_sessionmaker, 9108)

    async with sqlite_sessionmaker() as session:
        await redeem_gift_code(session, user_id=user_id, code_plaintext=code)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        wallet = await WalletRepo(session).get_or_create(user_id, currency="USD")
        assert wallet.balance_minor == 0
