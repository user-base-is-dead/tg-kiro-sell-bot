from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.catalog import StockItem, StockStatus


class StockRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_add(
        self, product_id: int, encrypted_payloads: list[str], *, batch_id: str, added_by_admin_id: int
    ) -> int:
        items = [
            StockItem(
                product_id=product_id,
                payload=payload,
                status=StockStatus.AVAILABLE,
                batch_id=batch_id,
                added_by_admin_id=added_by_admin_id,
            )
            for payload in encrypted_payloads
        ]
        self._session.add_all(items)
        await self._session.flush()
        return len(items)

    async def list_available(self, product_id: int, limit: int = 50) -> list[StockItem]:
        stmt = (
            select(StockItem)
            .where(StockItem.product_id == product_id, StockItem.status == StockStatus.AVAILABLE)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def void_available(self, stock_item_id: int) -> bool:
        item = await self._session.get(StockItem, stock_item_id)
        if item is None or item.status != StockStatus.AVAILABLE:
            return False
        item.status = StockStatus.VOID
        await self._session.flush()
        return True
