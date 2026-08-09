from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.catalog import Category


class CategoryRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self) -> list[Category]:
        result = await self._session.execute(
            select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order, Category.name)
        )
        return list(result.scalars().all())

    async def list_all(self) -> list[Category]:
        result = await self._session.execute(select(Category).order_by(Category.sort_order, Category.name))
        return list(result.scalars().all())

    async def get_by_id(self, category_id: int) -> Category | None:
        return await self._session.get(Category, category_id)

    async def get_by_slug(self, slug: str) -> Category | None:
        result = await self._session.execute(select(Category).where(Category.slug == slug))
        return result.scalar_one_or_none()

    async def create(self, **fields: object) -> Category:
        category = Category(**fields)
        self._session.add(category)
        await self._session.flush()
        return category

    async def delete(self, category: Category) -> None:
        await self._session.delete(category)
