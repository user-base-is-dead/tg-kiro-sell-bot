"""The product picker read as a flat alphabetical list of everything.

An admin recognises their catalog by where things sit — loose products out in the open, the rest in
folders, the way the store itself renders them. A flat list made them read names to find something
they could have pointed at, and with two pages of near-identical names ("Kiro Pro", "Kiro Pro Max",
"Kiro Pro Plus") that is a real chance of announcing the wrong product.
"""

from __future__ import annotations

import pytest

from app.bot.handlers.admin.broadcast import _page_nav, _product_button
from app.database.models.catalog import (
    Category,
    FulfillmentMode,
    Product,
    ProductStatus,
)
from app.database.repositories.category_repo import CategoryRepo
from app.database.repositories.product_repo import ProductRepo


async def _product(session, name: str, *, category_id: int | None, status=ProductStatus.IN_STOCK):
    product = Product(
        name=name,
        slug=name.lower().replace(" ", "-"),
        price_minor=1000,
        currency="USD",
        category_id=category_id,
        status=status,
        is_active=True,
        fulfillment_mode=FulfillmentMode.AUTO,
    )
    session.add(product)
    await session.flush()
    return product


@pytest.mark.asyncio
async def test_loose_products_are_reachable_without_opening_a_folder(sqlite_sessionmaker):
    """A product filed outside every category has no folder to be found in — the flat list was the
    only thing keeping it reachable, so grouping had to keep it in the open."""
    async with sqlite_sessionmaker() as session:
        category = Category(name="Software", slug="software", is_active=True)
        session.add(category)
        await session.flush()
        await _product(session, "Loose One", category_id=None)
        await _product(session, "Filed One", category_id=category.id)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        loose = await ProductRepo(session).list_uncategorized(active_only=True)
        assert [p.name for p in loose] == ["Loose One"]
        categories = await CategoryRepo(session).list_active()
        assert [c.name for c in categories] == ["Software"]
        assert await ProductRepo(session).count_by_category(categories[0].id) == 1


@pytest.mark.asyncio
async def test_a_folder_counts_only_what_is_inside_it(sqlite_sessionmaker):
    """The count on the folder is what tells an admin whether it is worth opening."""
    async with sqlite_sessionmaker() as session:
        empty = Category(name="Empty", slug="empty", is_active=True)
        full = Category(name="Full", slug="full", is_active=True)
        session.add_all([empty, full])
        await session.flush()
        for i in range(3):
            await _product(session, f"P{i}", category_id=full.id)
        await session.commit()
        empty_id, full_id = empty.id, full.id

    async with sqlite_sessionmaker() as session:
        repo = ProductRepo(session)
        assert await repo.count_by_category(empty_id) == 0
        assert await repo.count_by_category(full_id) == 3


@pytest.mark.asyncio
async def test_an_unreleased_product_is_marked_in_the_list(sqlite_sessionmaker):
    """It can be attached — it is the one an announcement is most likely to be about — but the
    admin has to be able to see which button it will carry before picking it."""
    async with sqlite_sessionmaker() as session:
        soon = await _product(session, "Kiro Pro Max", category_id=None, status=ProductStatus.COMING_SOON)
        live = await _product(session, "Kiro Pro", category_id=None)
        await session.commit()

        assert _product_button(soon)[0].text.startswith("🔜")
        assert _product_button(live)[0].text.startswith("🛍️")
        assert _product_button(live)[0].callback_data == f"broadcast_setprod:{live.id}"


def test_paging_only_offers_the_directions_that_exist() -> None:
    assert [b.text for b in _page_nav("t", 0, 1)] == []
    assert [b.text for b in _page_nav("t", 0, 3)] == ["Next ➡️"]
    assert [b.text for b in _page_nav("t", 1, 3)] == ["⬅️ Prev", "Next ➡️"]
    assert [b.text for b in _page_nav("t", 2, 3)] == ["⬅️ Prev"]
    assert _page_nav("broadcast_pickcat:4", 1, 3)[0].callback_data == "broadcast_pickcat:4:0"
