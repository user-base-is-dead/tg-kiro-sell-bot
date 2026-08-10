from __future__ import annotations

import pytest

from app.bot.keyboards.products import category_grid
from app.bot.keyboards.styles import PRIMARY
from app.database.models.catalog import Category, FulfillmentMode, Product, ProductStatus
from app.services.announcement_service import build_announcement
from app.services.catalog_service import ProductView, stock_label


def _product(**overrides) -> Product:
    fields = dict(
        id=1,
        name="Kiro Pro Max",
        price_minor=510,
        currency="USD",
        warranty_days=3,
        fulfillment_mode=FulfillmentMode.AUTO,
        status=ProductStatus.IN_STOCK,
        low_stock_threshold=3,
    )
    fields.update(overrides)
    return Product(**fields)


def test_every_category_button_is_blue() -> None:
    markup = category_grid([Category(id=i, name=f"C{i}", emoji="📦") for i in range(4)], "en")
    category_styles = [b.style for row in markup.inline_keyboard for b in row if b.callback_data.startswith("cat:")]
    assert category_styles == [PRIMARY] * 4


def test_stock_label_counts_an_auto_product() -> None:
    view = ProductView(_product(), 7, ProductStatus.IN_STOCK)
    assert stock_label(view) == "7 left"


def test_stock_label_is_empty_for_manual_products() -> None:
    # There is no pool behind a MANUAL product, so any number here would be a lie.
    view = ProductView(_product(fulfillment_mode=FulfillmentMode.MANUAL), 0, ProductStatus.IN_STOCK)
    assert stock_label(view) == ""


@pytest.mark.parametrize(
    ("kind", "headline"),
    [("new", "NEW PRODUCT"), ("restock", "BACK IN STOCK"), ("sold_out", "SOLD OUT")],
)
def test_each_announcement_kind_is_recognisable_from_its_headline(kind: str, headline: str) -> None:
    text = build_announcement(kind, _product(), 4)
    assert headline in text
    assert "Kiro Pro Max" in text


def test_sold_out_announcement_omits_price_and_stock() -> None:
    # Nothing to buy — a price and a count would only invite a wasted trip to the store.
    text = build_announcement("sold_out", _product(), 0)
    assert "5.10" not in text
    assert "available" not in text
