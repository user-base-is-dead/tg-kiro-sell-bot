from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.repositories.product_repo import ProductRepo
from app.locales.i18n import t


async def delivery_note(session: AsyncSession, product_id: int | None, locale: str) -> str:
    """The product's `delivery_info`, formatted for the message that hands the item over.

    `delivery_info` is the admin-authored "how to actually use this" text — change the password
    immediately, this key only works on Windows 10+, redeem it at this URL. It is the one thing a
    buyer needs alongside the payload, and for a long time it was write-only: the admin could set it
    and see it on the product screen, but no buyer-facing screen ever read it back.

    Returns "" when there is nothing to say, so callers can append it unconditionally.
    """
    if product_id is None:
        return ""
    product = await ProductRepo(session).get_by_id(product_id)
    info = (product.delivery_info or "").strip() if product else ""
    if not info:
        return ""
    return t("orders.delivery_info", locale, info=info)
