"""A product's own screen hid the number the admin had typed.

The "📦 Stock:" line branched on `fulfillment_mode` alone: any MANUAL product said "Made to order",
even when the admin had set a count on it by hand. Manually raising or lowering stock changed the
listing and the buy rules but not the one screen a shopper actually reads before buying — so a
product could say "made to order" while its owner had deliberately set it to 2 left.
"""

from __future__ import annotations

import pytest

from app.database.models.catalog import FulfillmentMode, Product, ProductStatus
from app.services.catalog_service import ProductView, stock_detail_line


def _view(*, stock: int, status=ProductStatus.IN_STOCK, mode=FulfillmentMode.AUTO, manual=None):
    return ProductView(
        Product(id=1, name="P", price_minor=100, currency="USD", fulfillment_mode=mode, manual_stock=manual),
        stock,
        status,
    )


def test_a_credential_backed_product_shows_its_count() -> None:
    assert "5 remaining" in stock_detail_line(_view(stock=5))


def test_a_hand_set_count_is_shown_on_a_manual_product() -> None:
    """The regression. `manual_stock` is the one figure a human deliberately typed."""
    line = stock_detail_line(_view(stock=2, mode=FulfillmentMode.MANUAL, manual=2))

    assert "2 remaining" in line
    assert "Made to order" not in line


def test_a_hand_set_zero_is_shown_rather_than_hidden() -> None:
    """Setting the override to zero is how sales are closed without disabling the product — telling
    the shopper "made to order" there invites an order that cannot be filled."""
    line = stock_detail_line(_view(stock=0, status=ProductStatus.OUT_OF_STOCK, mode=FulfillmentMode.MANUAL, manual=0))

    assert "0 remaining" in line


def test_made_to_order_survives_where_there_is_genuinely_no_number() -> None:
    """A MANUAL product with no override has no pool to count. Printing 0 there would be a zero
    that isn't one."""
    assert stock_detail_line(_view(stock=0, mode=FulfillmentMode.MANUAL)) == "📦 Stock: Made to order\n"


@pytest.mark.parametrize("status", [ProductStatus.COMING_SOON, ProductStatus.DISABLED])
def test_an_unreleased_product_claims_no_count_at_all(status) -> None:
    """The status line right below already says "Coming soon"; a "0 remaining" above it reads as
    sold out, which is a different and wrong thing to tell someone."""
    assert stock_detail_line(_view(stock=0, status=status)) == ""
