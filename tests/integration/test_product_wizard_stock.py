from __future__ import annotations

from app.database.models.catalog import FulfillmentMode, ProductStatus
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import add_stock, compute_display_status, create_product


async def test_a_product_created_with_stock_is_immediately_sellable(sqlite_sessionmaker) -> None:
    """Products used to be born OUT OF STOCK and needed a second trip to Manage Stock — which was
    itself crashing. Creating with stock has to land IN_STOCK in one pass."""
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session,
            category_id=None,
            name="Kiro Pro",
            description=None,
            price_minor=999,
            currency="USD",
            fulfillment_mode=FulfillmentMode.AUTO,
            warranty_days=0,
            delivery_info=None,
            image_file_id=None,
        )
        count = await add_stock(
            session,
            product_id=product_id,
            plaintext_payloads=["KEY-1", "KEY-2", "KEY-3"],
            added_by_admin_id=1,
        )
        await session.commit()

    assert count == 3

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        view = await compute_display_status(session, product)
        assert view.available_stock == 3
        assert view.display_status is not ProductStatus.OUT_OF_STOCK


async def test_skipping_stock_leaves_the_product_out_of_stock(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session,
            category_id=None,
            name="Empty Product",
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
        view = await compute_display_status(session, product)
        assert view.available_stock == 0


async def test_a_manual_product_is_sellable_with_no_stock_at_all(sqlite_sessionmaker) -> None:
    """The wizard skips the stock step for MANUAL, so this is the state it must be born in: live,
    not OUT OF STOCK. Asking for keys would be a step with no possible answer."""
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session,
            category_id=None,
            name="Hand Fulfilled",
            description=None,
            price_minor=999,
            currency="USD",
            fulfillment_mode=FulfillmentMode.MANUAL,
            warranty_days=0,
            delivery_info=None,
            image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        view = await compute_display_status(session, product)
        assert view.display_status is ProductStatus.IN_STOCK
