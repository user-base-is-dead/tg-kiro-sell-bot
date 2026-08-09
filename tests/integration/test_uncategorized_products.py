from __future__ import annotations

from app.database.models.catalog import FulfillmentMode
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import create_product


async def test_a_product_can_exist_with_no_category(sqlite_sessionmaker) -> None:
    """Choosing "Uncategorized" used to manufacture a real Category row, which then appeared as a
    folder to buyers. A product with no category must simply have none."""
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session,
            category_id=None,
            name="Loose Product",
            description=None,
            price_minor=999,
            currency="USD",
            fulfillment_mode=FulfillmentMode.AUTO,
            warranty_days=0,
            delivery_info=None,
            image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        assert product is not None
        assert product.category_id is None


async def test_uncategorized_products_are_listed_and_counted(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        for name in ("Loose A", "Loose B"):
            await create_product(
                session,
                category_id=None,
                name=name,
                description=None,
                price_minor=100,
                currency="USD",
                fulfillment_mode=FulfillmentMode.AUTO,
                warranty_days=0,
                delivery_info=None,
                image_file_id=None,
            )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        repo = ProductRepo(session)
        assert await repo.count_uncategorized(active_only=False) == 2
        names = {p.name for p in await repo.list_uncategorized(active_only=False)}
        assert names == {"Loose A", "Loose B"}


def test_migration_0012_chains_from_head() -> None:
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "app/database/migrations/versions/0012_product_category_nullable.py"
    )
    source = path.read_text(encoding="utf-8")

    assert 'revision = "0012"' in source
    assert 'down_revision = "0011"' in source
    assert "def upgrade()" in source and "def downgrade()" in source
