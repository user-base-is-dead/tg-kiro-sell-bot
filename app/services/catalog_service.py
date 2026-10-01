from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_cipher, normalize_stock_text, stock_fingerprint
from app.database.models.catalog import FulfillmentMode, Product, ProductStatus
from app.database.repositories.category_repo import CategoryRepo
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.stock_repo import StockRepo
from app.services import stock_hold_service


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "item"
    return f"{base}-{secrets.token_hex(2)}"


@dataclass(frozen=True)
class ProductView:
    """What screens render — status derived from live stock count, never trusted from callback
    data or a stale cache."""

    product: Product
    available_stock: int
    display_status: ProductStatus


def _threshold_status(available: int, product: Product) -> ProductStatus:
    return ProductStatus.LOW_STOCK if available <= product.low_stock_threshold else ProductStatus.IN_STOCK


async def compute_display_status(session: AsyncSession, product: Product) -> ProductView:
    if product.status in (ProductStatus.COMING_SOON, ProductStatus.DISABLED):
        return ProductView(product, 0, product.status)

    # The admin's hand-set count wins over every derived number, for both fulfilment modes. It is
    # the one figure a human deliberately typed, so a stock query cannot be allowed to contradict
    # it — including down to zero, which is how an override is used to close sales without
    # disabling the product.
    if product.manual_stock is not None:
        available = max(0, product.manual_stock)
        if available == 0:
            return ProductView(product, 0, ProductStatus.OUT_OF_STOCK)
        return ProductView(product, available, _threshold_status(available, product))

    if product.fulfillment_mode == FulfillmentMode.MANUAL:
        # MANUAL products aren't backed by a pre-added code pool — admin fulfills each order
        # by hand, so availability isn't gated by stock_items at all.
        return ProductView(product, 0, ProductStatus.IN_STOCK)

    available = await ProductRepo(session).available_stock_count(product.id)
    if available > 0:
        return ProductView(product, available, _threshold_status(available, product))

    # Nothing free — but "someone is mid-checkout on the last one" and "they are all sold" are
    # different facts for the shopper. The first un-does itself within the hold window, so they are
    # told to wait rather than turned away.
    held = await stock_hold_service.held_count(session, product.id)
    status = ProductStatus.ON_HOLD if held > 0 else ProductStatus.OUT_OF_STOCK
    return ProductView(product, 0, status)


def stock_detail_line(view: ProductView) -> str:
    """The "📦 Stock:" line on a product's own screen.

    Reads the same numbers the listing does. It used to branch on `fulfillment_mode` alone, so a
    MANUAL product said "Made to order" even when the admin had typed a count into it — the one
    figure a human deliberately set was the one the shopper could not see. "Made to order" is only
    honest when there is genuinely no number: a MANUAL product with no override.
    """
    if view.display_status in (ProductStatus.COMING_SOON, ProductStatus.DISABLED):
        # The status line right below already says "Coming soon"; a "0 remaining" above it reads as
        # sold out, which is a different (and wrong) thing to tell someone.
        return ""
    if view.product.manual_stock is None and view.product.fulfillment_mode == FulfillmentMode.MANUAL:
        return "📦 Stock: made to order\n"
    return f"📦 Stock: <b>{view.available_stock} available</b>\n"


def stock_label(view: ProductView) -> str:
    """How many are left, in the shopper's words — "" when the number would be meaningless.

    MANUAL products have no pool to count, and a COMING_SOON/DISABLED one is not for sale, so both
    would only be advertising a zero that means nothing.
    """
    if view.display_status in (ProductStatus.COMING_SOON, ProductStatus.DISABLED):
        return ""
    # An override is a real count even on a MANUAL product — that is the whole point of typing one.
    if view.product.manual_stock is None and view.product.fulfillment_mode == FulfillmentMode.MANUAL:
        return ""
    if view.available_stock <= 0:
        return "0 left"
    return f"{view.available_stock} left"


async def create_category(
    session: AsyncSession,
    *,
    name: str,
    emoji: str | None,
    description: str | None,
    image_file_id: str | None,
) -> int:
    slug = slugify(name)
    category = await CategoryRepo(session).create(
        name=name, slug=slug, emoji=emoji, description=description, image_file_id=image_file_id
    )
    return category.id


async def create_product(
    session: AsyncSession,
    *,
    category_id: int | None,
    name: str,
    description: str | None,
    price_minor: int,
    currency: str,
    fulfillment_mode: FulfillmentMode,
    warranty_days: int,
    delivery_info: str | None,
    image_file_id: str | None,
    manual_stock: int | None = None,
    low_stock_threshold: int = 3,
) -> int:
    slug = slugify(name)
    product = await ProductRepo(session).create(
        category_id=category_id,
        name=name,
        slug=slug,
        description=description,
        price_minor=price_minor,
        currency=currency,
        fulfillment_mode=fulfillment_mode,
        warranty_days=warranty_days,
        delivery_info=delivery_info,
        image_file_id=image_file_id,
        manual_stock=manual_stock,
        low_stock_threshold=low_stock_threshold,
        status=ProductStatus.OUT_OF_STOCK,
    )
    # A hand-set count is live the moment it is typed — leaving the row at the OUT_OF_STOCK default
    # would make the sell-out latch fire a "sold out" announcement for a product that never sold
    # anything.
    if manual_stock:
        product.status = _threshold_status(manual_stock, product)
    return product.id


# The longest plaintext one stock item may hold. The ciphertext column is VARCHAR(4096), and Fernet
# grows its input by about a third plus a fixed header — past ~3000 bytes PostgreSQL rejects the row
# and takes the whole batch down with it. Refused up front instead, with the reason on screen.
MAX_PAYLOAD_BYTES = 3000


@dataclass(frozen=True)
class StockAddResult:
    """What one Add Stock batch did, so the screen can say why fewer items went in than were sent."""

    added: int
    # Already in stock, already sold (in this product or any other), or repeated within the batch.
    duplicates: int = 0
    too_long: int = 0


async def add_stock(
    session: AsyncSession, *, product_id: int, plaintext_payloads: list[str], added_by_admin_id: int
) -> StockAddResult:
    """Encrypts every payload before it touches the DB — a dump alone never leaks sellable goods.

    And, by default, refuses every login the store has seen before. One credential is one sale: the
    same login uploaded twice would otherwise sit on the shelf as two items and reach two buyers.
    Duplicates are recognised by fingerprint (`security.stock_fingerprint`), across every product and
    every status, sold ones included — a login already delivered to somebody is never put on sale
    again.

    A product with `allow_duplicate_stock` on is the deliberate exception: a shared account is one
    login sold to many people, so every copy is the same text and the rule above would reject all but
    the first. For those, the fingerprint is not computed and `content_hash` is stored NULL — exempt
    from the unique index — so the guarantee stays exactly as strict for every other product.
    """
    cipher = get_cipher()
    product = await ProductRepo(session).get_by_id(product_id)
    allow_duplicates = product is not None and product.allow_duplicate_stock

    seen: set[str] = set()
    fresh: list[tuple[str, str | None]] = []
    duplicates = too_long = 0
    for payload in plaintext_payloads:
        payload = payload.strip()
        if not normalize_stock_text(payload):
            continue
        if len(payload.encode()) > MAX_PAYLOAD_BYTES:
            too_long += 1
            continue
        if allow_duplicates:
            # No fingerprint at all, so nothing to compare against and nothing to collide with —
            # including the identical line twice in this very paste, which for a shared account is
            # the admin saying "sell it to two people".
            fresh.append((payload, None))
            continue
        fingerprint = stock_fingerprint(payload)
        if fingerprint in seen:
            duplicates += 1
            continue
        seen.add(fingerprint)
        fresh.append((payload, fingerprint))

    repo = StockRepo(session)
    rows: list[tuple[str, str | None]]
    if allow_duplicates:
        rows = [(cipher.encrypt(payload), None) for payload, _ in fresh]
    else:
        taken = await repo.existing_fingerprints([fp for _, fp in fresh if fp is not None])
        rows = [(cipher.encrypt(payload), fp) for payload, fp in fresh if fp not in taken]
        duplicates += len(fresh) - len(rows)

    added = 0
    if rows:
        added = await repo.bulk_add(
            product_id, rows, batch_id=secrets.token_hex(4), added_by_admin_id=added_by_admin_id
        )

    if product is not None:
        await resync_status(session, product)

    return StockAddResult(added=added, duplicates=duplicates, too_long=too_long)


async def resync_status(session: AsyncSession, product: Product) -> None:
    """Write the computed status back onto the row.

    `product.status` is both a cache and the latch the sell-out announcement keys off, so anything
    that changes availability — loading credentials, retyping the hand-set count — has to bring it
    back in line or the store advertises a state that stopped being true. COMING_SOON and DISABLED
    are admin decisions, never overwritten by a stock count.
    """
    if product.status in (ProductStatus.COMING_SOON, ProductStatus.DISABLED):
        return
    view = await compute_display_status(session, product)
    product.status = view.display_status
