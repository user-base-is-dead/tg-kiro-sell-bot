from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.models.catalog import FulfillmentMode, Product, ProductStatus
from app.database.repositories.product_repo import ProductRepo
from app.services.broadcast_service import create_broadcast, run_worker
from app.services.catalog_service import compute_display_status

logger = logging.getLogger(__name__)

# Three events, three shapes. A shopper who has seen one of these before should be able to tell
# which kind it is from the first line alone, without reading the product name.
_NEW = "new"
_RESTOCK = "restock"
_SOLD_OUT = "sold_out"


def _price(product: Product) -> str:
    return f"{product.price_minor / 100:.2f} {product.currency}"


def _stock_line(product: Product, available: int) -> str:
    if product.fulfillment_mode == FulfillmentMode.MANUAL:
        return "📦 Made to order\n"
    return f"📦 <b>{available} available</b>\n"


def build_announcement(kind: str, product: Product, available: int) -> str:
    """The message body users receive. Kept out of the send path so tests can read it directly."""
    warranty = f"🛡️ Warranty: {product.warranty_days} days\n" if product.warranty_days else ""

    if kind == _NEW:
        return (
            "━━━━━━━━━━━━━━━━━━\n"
            "🆕 <b>NEW PRODUCT</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"🛍️ <b>{product.name}</b>\n\n"
            f"💰 Price: {_price(product)}\n"
            f"{warranty}"
            f"{_stock_line(product, available)}"
            "\nJust added to the store — open /products to grab it."
        )

    if kind == _RESTOCK:
        return (
            "━━━━━━━━━━━━━━━━━━\n"
            "🔄 <b>BACK IN STOCK</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"🛍️ <b>{product.name}</b> is available again.\n\n"
            f"💰 Price: {_price(product)}\n"
            f"{warranty}"
            f"{_stock_line(product, available)}"
            "\nIt sold out once already — open /products before it does again."
        )

    return (
        "━━━━━━━━━━━━━━━━━━\n"
        "🔴 <b>SOLD OUT</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🛍️ <b>{product.name}</b> is now out of stock.\n\n"
        "No need to keep checking — you'll get a <b>BACK IN STOCK</b> message here the moment "
        "it returns."
    )


async def send_announcement(
    bot: Bot, session: AsyncSession, *, kind: str, product: Product, admin_id: int
) -> int:
    """Queue one announcement to every active user. Returns the number of targets.

    Rides the existing broadcast machinery rather than DM-ing in a loop, so an announcement is
    rate-limited, resumable after a restart, and visible in /broadcast_status like any other.
    """
    view = await compute_display_status(session, product)
    body = build_announcement(kind, product, view.available_stock)

    broadcast = await create_broadcast(
        session,
        admin_id=admin_id,
        title=f"{kind}: {product.name}"[:60],
        body=body,
    )
    await session.flush()

    settings = get_settings()
    asyncio.create_task(run_worker(bot, settings.database_url, broadcast.id))  # noqa: RUF006 — fire-and-forget worker
    return broadcast.total_targets


async def announce_new_product(bot: Bot, session: AsyncSession, product: Product, admin_id: int) -> int:
    return await send_announcement(bot, session, kind=_NEW, product=product, admin_id=admin_id)


async def announce_restock(bot: Bot, session: AsyncSession, product: Product, admin_id: int) -> int:
    return await send_announcement(bot, session, kind=_RESTOCK, product=product, admin_id=admin_id)


async def maybe_announce_sold_out(bot: Bot, session: AsyncSession, product_id: int) -> bool:
    """Announce a sell-out, at most once per sell-out, without asking anybody.

    Unlike the other two this fires on its own: the admin isn't in the loop when a buyer takes the
    last item, and waiting for them to approve a message about something that already happened
    would only make it arrive late.

    `product.status` is the latch. It is the persisted column, not the computed view, and only
    `add_stock` moves it back off OUT_OF_STOCK — so the second buyer to hit an empty shelf finds it
    already flipped and no second announcement goes out. ON_HOLD is deliberately not a sell-out:
    those items come back by themselves when the hold expires.
    """
    product = await ProductRepo(session).get_by_id(product_id)
    if product is None or not product.is_active:
        return False
    if product.status in (ProductStatus.COMING_SOON, ProductStatus.DISABLED, ProductStatus.OUT_OF_STOCK):
        return False
    if product.fulfillment_mode == FulfillmentMode.MANUAL:
        return False

    view = await compute_display_status(session, product)
    if view.display_status is not ProductStatus.OUT_OF_STOCK:
        return False

    product.status = ProductStatus.OUT_OF_STOCK
    await session.flush()

    try:
        await send_announcement(bot, session, kind=_SOLD_OUT, product=product, admin_id=0)
    except Exception:  # noqa: BLE001 — a purchase must never fail over an announcement
        logger.exception("sold-out announcement failed for product %s", product_id)
        return False
    return True
