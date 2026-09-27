from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.catalog import StockItem, StockStatus
from app.database.models.order import Order, OrderItem
from app.database.models.user import User

# The two halves of a product's stock as the admin sees them. Unsold is everything still on the
# shelf, including a credential somebody is holding at checkout right now; sold is everything that
# has left it, including the few seconds a credential spends RESERVED inside a purchase.
UNSOLD = (StockStatus.AVAILABLE, StockStatus.HELD)
SOLD = (StockStatus.RESERVED, StockStatus.DELIVERED)

# SQLite caps bound parameters per statement; a paste of a few hundred logins must not hit it.
_IN_CHUNK = 500


class StockRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_add(
        self,
        product_id: int,
        rows: list[tuple[str, str]],
        *,
        batch_id: str,
        added_by_admin_id: int,
    ) -> int:
        """Insert (ciphertext, fingerprint) pairs as AVAILABLE. The caller has already deduped them;
        the unique index on `content_hash` is the backstop if two admins race with the same login."""
        items = [
            StockItem(
                product_id=product_id,
                payload=payload,
                content_hash=fingerprint,
                status=StockStatus.AVAILABLE,
                batch_id=batch_id,
                added_by_admin_id=added_by_admin_id,
            )
            for payload, fingerprint in rows
        ]
        self._session.add_all(items)
        await self._session.flush()
        return len(items)

    async def existing_fingerprints(self, fingerprints: list[str]) -> set[str]:
        """Which of these fingerprints are already in stock_items — any product, any status."""
        found: set[str] = set()
        for start in range(0, len(fingerprints), _IN_CHUNK):
            chunk = fingerprints[start : start + _IN_CHUNK]
            result = await self._session.execute(
                select(StockItem.content_hash).where(StockItem.content_hash.in_(chunk))
            )
            found.update(result.scalars().all())
        return found

    async def find_by_fingerprint(self, fingerprint: str) -> StockItem | None:
        result = await self._session.execute(
            select(StockItem).where(StockItem.content_hash == fingerprint).limit(1)
        )
        return result.scalars().first()

    async def get(self, stock_item_id: int) -> StockItem | None:
        return await self._session.get(StockItem, stock_item_id, populate_existing=True)

    async def count_by_statuses(self, product_id: int, statuses) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(StockItem)
            .where(StockItem.product_id == product_id, StockItem.status.in_(statuses))
        )
        return int(result.scalar_one())

    async def list_unsold(self, product_id: int, *, offset: int, limit: int) -> list[StockItem]:
        """Unsold credentials, oldest first — the order they are sold in."""
        result = await self._session.execute(
            select(StockItem)
            .where(StockItem.product_id == product_id, StockItem.status.in_(UNSOLD))
            .order_by(StockItem.id)
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_sold(
        self, product_id: int, *, offset: int, limit: int
    ) -> list[tuple[StockItem, Order | None, User | None]]:
        """Sold credentials, newest sale first, each with the order and buyer it went to — the
        answer to "who got this login?" when a buyer says it doesn't work."""
        result = await self._session.execute(
            select(StockItem, Order, User)
            .outerjoin(OrderItem, OrderItem.id == StockItem.order_item_id)
            .outerjoin(Order, Order.id == OrderItem.order_id)
            .outerjoin(User, User.id == Order.user_id)
            .where(StockItem.product_id == product_id, StockItem.status.in_(SOLD))
            .order_by(StockItem.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [(item, order, user) for item, order, user in result.all()]

    async def sale_of(self, item: StockItem) -> tuple[Order | None, User | None]:
        """The order and buyer a sold credential went to."""
        if item.order_item_id is None:
            return None, None
        result = await self._session.execute(
            select(Order, User)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .outerjoin(User, User.id == Order.user_id)
            .where(OrderItem.id == item.order_item_id)
        )
        row = result.first()
        return (row[0], row[1]) if row else (None, None)
