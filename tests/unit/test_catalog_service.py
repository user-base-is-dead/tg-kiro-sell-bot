from __future__ import annotations

import pytest

from app.database.models.catalog import FulfillmentMode, Product, ProductStatus
from app.services.catalog_service import compute_display_status, slugify


def test_slugify_lowercases_and_dashes():
    slug = slugify("Premium Gift Card!!")
    assert slug.startswith("premium-gift-card-")


def test_slugify_never_empty():
    slug = slugify("!!!")
    assert slug.startswith("item-")


def test_slugify_is_unique_per_call():
    a = slugify("Same Name")
    b = slugify("Same Name")
    assert a != b  # random suffix guarantees uniqueness even for identical product names


@pytest.mark.parametrize("status", [ProductStatus.COMING_SOON, ProductStatus.DISABLED])
async def test_manual_override_statuses_never_touch_stock(status):
    product = Product(id=1, status=status, fulfillment_mode=FulfillmentMode.AUTO, low_stock_threshold=3)
    # session=None would blow up if the function tried to query stock — proves the early return.
    view = await compute_display_status(session=None, product=product)
    assert view.display_status == status
    assert view.available_stock == 0


async def test_manual_fulfillment_is_always_in_stock_unless_overridden():
    product = Product(id=1, status=ProductStatus.OUT_OF_STOCK, fulfillment_mode=FulfillmentMode.MANUAL, low_stock_threshold=3)
    view = await compute_display_status(session=None, product=product)
    assert view.display_status == ProductStatus.IN_STOCK
