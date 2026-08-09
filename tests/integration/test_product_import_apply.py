from __future__ import annotations

from app.database.models.catalog import FulfillmentMode
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import create_product
from app.services.product_import import apply_rows, parse_csv

HEADER = "id,name,category,price,currency,mode,warranty,description,delivery_info,active"


async def test_new_names_are_created(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        report = await apply_rows(session, parse_csv(f"{HEADER}\n,Kiro Pro,,9.99,,,,,,").rows)
        await session.commit()

    assert (report.created, report.updated) == (1, 0)


async def test_an_existing_name_is_updated_not_duplicated(sqlite_sessionmaker) -> None:
    """Upsert is the whole point — re-uploading an edited export must not double the catalogue."""
    async with sqlite_sessionmaker() as session:
        await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=999,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        report = await apply_rows(session, parse_csv(f"{HEADER}\n,kiro pro,,19.99,,,,,,").rows)
        await session.commit()

    assert (report.created, report.updated) == (0, 1)

    async with sqlite_sessionmaker() as session:
        products = await ProductRepo(session).list_uncategorized(active_only=False)
        assert len(products) == 1
        assert products[0].price_minor == 1999, "match is case-insensitive on name"


async def test_a_stale_id_is_an_error_not_a_silent_create(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        report = await apply_rows(session, parse_csv(f"{HEADER}\n9001,Ghost,,1.00,,,,,,").rows)
        await session.commit()

    assert report.created == 0
    assert any("9001" in e for e in report.errors)


async def test_a_missing_category_is_created_and_reported(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        report = await apply_rows(session, parse_csv(f"{HEADER}\n,Kiro Pro,Gaming,9.99,,,,,,").rows)
        await session.commit()

    assert report.categories_created == ["Gaming"]


async def test_export_round_trips_as_a_no_op(sqlite_sessionmaker) -> None:
    """Export → edit in Excel → re-upload is the documented bulk-edit path, so an unedited export
    must update everything and create nothing."""
    from app.services.product_import import to_csv

    async with sqlite_sessionmaker() as session:
        await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=999,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        products = await ProductRepo(session).list_uncategorized(active_only=False)
        text = to_csv(products, {})

    async with sqlite_sessionmaker() as session:
        parsed = parse_csv(text)
        assert parsed.errors == []
        report = await apply_rows(session, parsed.rows)
        await session.commit()

    assert (report.created, report.updated) == (0, 1)
