"""`delivery_info` is the "how to actually use this" text the admin writes on a product.

It was write-only for a long time: settable on the admin screen, carried through CSV import/export,
shown back to the admin — and read by no buyer-facing screen at all. A buyer got the bare payload
and a warranty line, and the instructions the seller wrote never reached them.
"""

from __future__ import annotations

import pytest

from app.bot.delivery_notes import delivery_note
from app.core.security import get_cipher
from app.database.models.catalog import (
    FulfillmentMode,
    Product,
    ProductStatus,
    StockItem,
    StockStatus,
)
from app.locales.i18n import t


async def _make_product(sessionmaker, *, delivery_info: str | None, stock: int = 1) -> int:
    async with sessionmaker() as session:
        product = Product(
            category_id=None,
            name="Kiro Pro",
            slug="kiro-pro",
            price_minor=500,
            currency="USD",
            status=ProductStatus.IN_STOCK,
            fulfillment_mode=FulfillmentMode.AUTO,
            warranty_days=1,
            is_active=True,
            delivery_info=delivery_info,
        )
        session.add(product)
        await session.flush()
        for _ in range(stock):
            session.add(
                StockItem(
                    product_id=product.id,
                    payload=get_cipher().encrypt("KEY-1"),
                    status=StockStatus.AVAILABLE,
                )
            )
        await session.commit()
        return product.id


@pytest.mark.asyncio
async def test_the_note_carries_what_the_admin_wrote(sqlite_sessionmaker):
    product_id = await _make_product(
        sqlite_sessionmaker, delivery_info="Change the password immediately."
    )

    async with sqlite_sessionmaker() as session:
        note = await delivery_note(session, product_id, "en")

    assert "Change the password immediately." in note


@pytest.mark.asyncio
async def test_a_product_without_instructions_adds_nothing(sqlite_sessionmaker):
    """Callers append the note unconditionally, so "nothing to say" has to be an empty string
    rather than a stray header with a blank body."""
    product_id = await _make_product(sqlite_sessionmaker, delivery_info=None)

    async with sqlite_sessionmaker() as session:
        assert await delivery_note(session, product_id, "en") == ""


@pytest.mark.asyncio
async def test_whitespace_only_instructions_add_nothing(sqlite_sessionmaker):
    product_id = await _make_product(sqlite_sessionmaker, delivery_info="   \n  ")

    async with sqlite_sessionmaker() as session:
        assert await delivery_note(session, product_id, "en") == ""


@pytest.mark.asyncio
async def test_a_deleted_product_does_not_break_the_note(sqlite_sessionmaker):
    """`order_items.product_id` is NULL once the product is deleted (migration 0013), and an old
    order can still be rendered."""
    async with sqlite_sessionmaker() as session:
        assert await delivery_note(session, None, "en") == ""
        assert await delivery_note(session, 999_999, "en") == ""


def test_the_manual_pending_message_exists() -> None:
    """A MANUAL product's buyer used to be shown the literal string `orders.manual_pending` — the
    key was referenced by the checkout handler but never added to the locale file."""
    rendered = t("orders.manual_pending", "en")

    assert rendered != "orders.manual_pending", "the key must resolve to real text"
    assert "Orders" in rendered, "it should point the buyer at where to track it"


def test_the_delivery_info_template_exists() -> None:
    rendered = t("orders.delivery_info", "en", info="DO THE THING")

    assert rendered != "orders.delivery_info"
    assert "DO THE THING" in rendered
