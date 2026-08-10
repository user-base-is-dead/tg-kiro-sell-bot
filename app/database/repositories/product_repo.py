from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.catalog import Product, StockItem, StockStatus
from app.database.models.order import OrderItem


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

    async def list_uncategorized(
        self, *, offset: int = 0, limit: int = 50, active_only: bool = True
    ) -> list[Product]:
        """Products filed outside every category. They render above the folders in the store."""
        stmt = select(Product).where(Product.category_id.is_(None))
        if active_only:
            stmt = stmt.where(Product.is_active.is_(True))
        stmt = stmt.order_by(Product.sort_order, Product.name).offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_uncategorized(self, *, active_only: bool = True) -> int:
        stmt = select(func.count()).select_from(Product).where(Product.category_id.is_(None))
        if active_only:
            stmt = stmt.where(Product.is_active.is_(True))
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def count_all(self, *, name_like: str | None = None) -> int:
        """Counted in SQL. The admin list used to SELECT every product into Python to len() it."""
        stmt = select(func.count()).select_from(Product)
        if name_like:
            stmt = stmt.where(Product.name.ilike(f"%{name_like}%"))
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def list_page(
        self, *, offset: int, limit: int, name_like: str | None = None
    ) -> list[Product]:
        stmt = select(Product)
        if name_like:
            stmt = stmt.where(Product.name.ilike(f"%{name_like}%"))
        stmt = stmt.order_by(Product.id).offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_active(self, *, limit: int = 50) -> list[Product]:
        """Every buyable product, categorised or not. Used where a category is irrelevant — picking
        a product to give away, for instance — so that a loose product is not silently unreachable.
        """
        stmt = (
            select(Product)
            .where(Product.is_active.is_(True))
            .order_by(Product.sort_order, Product.name)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

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
        """Delete the product for real, detaching the history that outlives it.

        Everything pointing at a product falls into one of three buckets, and the whole point of
        this method is that none of them can block the delete or lose a buyer's data:

          * **keep, detached** — `order_items` and *sold* `stock_items`. Both snapshot what they
            need (name/price/warranty; the delivered payload), so NULLing the link leaves the
            buyer's order history and their warranty-time redelivery intact;
          * **delete** — *unsold* `stock_items`, held ones included. Unsold keys for a product
            nobody can buy any more are dead weight, and a live hold is a 5-minute reservation on a
            product that is going away;

        Gift codes are deliberately absent from that list: a gift carries its own items and never
        points at a catalog product, so deleting a product cannot break one.
        """
        product_id = product.id

        await self._session.execute(
            update(OrderItem).where(OrderItem.product_id == product_id).values(product_id=None)
        )
        await self._session.execute(
            delete(StockItem).where(
                StockItem.product_id == product_id,
                StockItem.status.in_((StockStatus.AVAILABLE, StockStatus.HELD)),
            )
        )
        await self._session.execute(
            update(StockItem).where(StockItem.product_id == product_id).values(product_id=None)
        )
        await self._session.delete(product)

    async def available_stock_count(self, product_id: int) -> int:
        """Credentials a new buyer could take right now.

        HELD credentials are excluded — that is the whole point of a hold — but a hold whose window
        has already closed counts as available again immediately, without waiting for the expiry
        sweep to relabel the row. Otherwise a product reads as unavailable for up to one job
        interval after the last hold lapsed, which is the buyer being told to wait for something
        that is already free.
        """
        now = datetime.now(UTC)
        stmt = (
            select(func.count())
            .select_from(StockItem)
            .where(
                StockItem.product_id == product_id,
                or_(
                    StockItem.status == StockStatus.AVAILABLE,
                    and_(StockItem.status == StockStatus.HELD, StockItem.held_until <= now),
                ),
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())
