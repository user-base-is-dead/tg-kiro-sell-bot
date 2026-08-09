"""Seeds a scratch/dev database with a couple of categories and products so there's
something to click through immediately after `alembic upgrade head`. Safe to re-run —
skips anything whose slug already exists."""
from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.database.models.catalog import FulfillmentMode, Product, ProductStatus
from app.database.repositories.category_repo import CategoryRepo
from app.database.repositories.product_repo import ProductRepo
from app.database.session import build_engine, build_sessionmaker, session_scope
from app.services.catalog_service import add_stock


async def seed() -> None:
    settings = get_settings()
    engine = build_engine(settings.database_url)
    sessionmaker = build_sessionmaker(engine)

    async with session_scope(sessionmaker) as session:
        cat_repo = CategoryRepo(session)
        prod_repo = ProductRepo(session)

        gaming = await cat_repo.get_by_slug("gaming")
        if gaming is None:
            gaming = await cat_repo.create(name="Gaming", slug="gaming", emoji="🎮", is_active=True, sort_order=1)
            print(f"created category: {gaming.name}")

        software = await cat_repo.get_by_slug("software")
        if software is None:
            software = await cat_repo.create(name="Software", slug="software", emoji="💻", is_active=True, sort_order=2)
            print(f"created category: {software.name}")

        key = await prod_repo.get_by_slug("premium-game-key-demo")
        if key is None:
            product = Product(
                category_id=gaming.id,
                name="Premium Game Key",
                slug="premium-game-key-demo",
                description="A demo AUTO-fulfillment product — buying it instantly delivers a code from stock.",
                price_minor=999,
                currency=settings.default_currency,
                status=ProductStatus.OUT_OF_STOCK,
                fulfillment_mode=FulfillmentMode.AUTO,
                warranty_days=7,
                is_active=True,
            )
            session.add(product)
            await session.flush()
            await add_stock(
                session,
                product_id=product.id,
                plaintext_payloads=["DEMO-KEY-AAAA-1111", "DEMO-KEY-BBBB-2222", "DEMO-KEY-CCCC-3333"],
                added_by_admin_id=0,
            )
            print(f"created product: {product.name} (+3 stock)")

        custom = await prod_repo.get_by_slug("custom-setup-demo")
        if custom is None:
            product = Product(
                category_id=software.id,
                name="Custom Setup Service",
                slug="custom-setup-demo",
                description="A demo MANUAL-fulfillment product — an admin fulfills each order by hand.",
                price_minor=2500,
                currency=settings.default_currency,
                status=ProductStatus.IN_STOCK,
                fulfillment_mode=FulfillmentMode.MANUAL,
                warranty_days=30,
                is_active=True,
            )
            session.add(product)
            await session.flush()
            print(f"created product: {product.name}")

    await engine.dispose()
    print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
