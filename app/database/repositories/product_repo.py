from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.catalog import Product, StockItem, StockStatus


class ProductRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_category(
        self, category_id: int, *, active_only: bool = True, offset: int = 0, limit: int = 8
    ) -> list[Product]:
        stmt = select(Product).where(Product.category_id == category_id)
        if active_only:
            stmt = stmt.where(Product.is_active.is_(True))
        stmt = stmt.order_by(Product.sort_order, Product.name).offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_category(self, category_id: int, *, active_only: bool = True) -> int:
        stmt = select(func.count()).select_from(Product).where(Product.category_id == category_id)
        if active_only:
            stmt = stmt.where(Product.is_active.is_(True))
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def get_by_id(self, product_id: int) -> Product | None:
        return await self._session.get(Product, product_id)

    async def get_by_slug(self, slug: str) -> Product | None:
        result = await self._session.execute(select(Product).where(Product.slug == slug))
        return result.scalar_one_or_none()

    async def create(self, **fields: object) -> Product:
        product = Product(**fields)
        self._session.add(product)
        await self._session.flush()
        return product

    async def delete(self, product: Product) -> None:
        await self._session.delete(product)

    async def available_stock_count(self, product_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(StockItem)
            .where(StockItem.product_id == product_id, StockItem.status == StockStatus.AVAILABLE)
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())
