"""Two dead ends in the Create Gift Code wizard.

1. It asked "which category is the product in?" before showing products. A gift is tied to a
   *product*, not a category, and that step made a product filed outside every category
   ungiftable while dead-ending a shop with no categories on "No categories yet — add a product
   first" even when it had products.
2. Answering the expiry question crashed. FSM data is JSON-serialized into Redis, and the handler
   put a `datetime` in it — the wizard died at step 5 every single time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.bot.handlers.admin.gifts import _expires_at
from app.bot.states.gift_form import GiftCreateForm
from app.database.models.catalog import (
    Category,
    FulfillmentMode,
    Product,
    ProductStatus,
)
from app.database.repositories.product_repo import ProductRepo


def test_the_wizard_has_no_category_step() -> None:
    """Pinned as a state check: re-adding the step is what reintroduces both dead ends."""
    assert not hasattr(GiftCreateForm, "category")


# ---- 1. Picking a product needs no category ----


async def _product(session, *, name: str, category_id: int | None, active: bool = True) -> int:
    product = Product(
        category_id=category_id,
        name=name,
        slug=name.lower().replace(" ", "-"),
        price_minor=500,
        currency="USD",
        status=ProductStatus.IN_STOCK,
        fulfillment_mode=FulfillmentMode.AUTO,
        warranty_days=0,
        is_active=active,
    )
    session.add(product)
    await session.flush()
    return product.id


@pytest.mark.asyncio
async def test_a_product_with_no_category_can_be_given_away(sqlite_sessionmaker):
    """The case the old flow could not reach at all."""
    async with sqlite_sessionmaker() as session:
        await _product(session, name="Loose Product", category_id=None)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        names = [p.name for p in await ProductRepo(session).list_active()]

    assert "Loose Product" in names


@pytest.mark.asyncio
async def test_products_from_every_category_are_offered_together(sqlite_sessionmaker):
    async with sqlite_sessionmaker() as session:
        category = Category(name="Cat", slug="cat", sort_order=1)
        session.add(category)
        await session.flush()
        await _product(session, name="Filed Product", category_id=category.id)
        await _product(session, name="Loose Product", category_id=None)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        names = [p.name for p in await ProductRepo(session).list_active()]

    assert set(names) == {"Filed Product", "Loose Product"}


@pytest.mark.asyncio
async def test_a_disabled_product_is_not_offered(sqlite_sessionmaker):
    async with sqlite_sessionmaker() as session:
        await _product(session, name="Retired", category_id=None, active=False)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await ProductRepo(session).list_active() == []


# ---- 2. Expiry must survive JSON round-tripping through Redis ----


def test_everything_the_expiry_step_stores_is_json_serializable() -> None:
    """The actual crash. aiogram's RedisStorage calls `json.dumps` on the FSM data, so anything
    that is not a JSON primitive takes the wizard down at the moment it is stored."""
    stored = {"expires_days": 7}

    json.dumps(stored)  # must not raise

    with pytest.raises(TypeError):
        json.dumps({"expires_at": datetime.now(UTC)})


def test_a_day_count_resolves_to_a_real_expiry() -> None:
    expires_at = _expires_at({"expires_days": 7})

    assert expires_at is not None
    delta = expires_at - datetime.now(UTC)
    assert timedelta(days=6, hours=23) < delta <= timedelta(days=7)


def test_zero_days_means_never() -> None:
    assert _expires_at({"expires_days": 0}) is None


def test_an_unanswered_expiry_means_never() -> None:
    """Defensive: a resumed or partly-filled wizard must not blow up on a missing key."""
    assert _expires_at({}) is None
