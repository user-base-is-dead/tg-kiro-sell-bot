from __future__ import annotations

from app.database.models.catalog import FulfillmentMode
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import create_product


async def _seed(sessionmaker, names: list[str]) -> None:
    async with sessionmaker() as session:
        for name in names:
            await create_product(
                session, category_id=None, name=name, description=None, price_minor=100,
                currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
                delivery_info=None, image_file_id=None,
            )
        await session.commit()


async def test_count_does_not_load_every_row(sqlite_sessionmaker) -> None:
    """_render_list used to SELECT every product into Python just to len() it, then slice ten."""
    await _seed(sqlite_sessionmaker, [f"Product {i}" for i in range(25)])

    async with sqlite_sessionmaker() as session:
        assert await ProductRepo(session).count_all() == 25


async def test_page_returns_only_its_slice(sqlite_sessionmaker) -> None:
    await _seed(sqlite_sessionmaker, [f"Product {i:02d}" for i in range(25)])

    async with sqlite_sessionmaker() as session:
        page = await ProductRepo(session).list_page(offset=10, limit=10)
        assert len(page) == 10


async def test_search_filters_by_name(sqlite_sessionmaker) -> None:
    await _seed(sqlite_sessionmaker, ["Kiro Pro", "Kiro Lite", "Other Thing"])

    async with sqlite_sessionmaker() as session:
        repo = ProductRepo(session)
        assert await repo.count_all(name_like="kiro") == 2
        names = {p.name for p in await repo.list_page(offset=0, limit=10, name_like="kiro")}
        assert names == {"Kiro Pro", "Kiro Lite"}
