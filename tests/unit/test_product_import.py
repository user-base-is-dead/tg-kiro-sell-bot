from __future__ import annotations

import pytest

from app.database.models.catalog import FulfillmentMode
from app.services.product_import import MAX_ROWS, parse_csv

HEADER = "id,name,category,price,currency,mode,warranty,description,delivery_info,active"


def test_minimal_row_uses_defaults() -> None:
    result = parse_csv(f"{HEADER}\n,Kiro Pro,,9.99,,,,,,")

    assert result.errors == []
    row = result.rows[0]
    assert row.name == "Kiro Pro"
    assert row.price_minor == 999
    assert row.currency == "USD"
    assert row.mode is FulfillmentMode.AUTO
    assert row.warranty_days == 0
    assert row.is_active is True
    assert row.category is None, "a blank category means no category, not a folder named ''"


def test_full_row_is_parsed() -> None:
    result = parse_csv(f"{HEADER}\n14,Kiro Lite,Software,4.99,EUR,manual,30,Cheap tier,Log in,no")

    row = result.rows[0]
    assert (row.product_id, row.category, row.currency) == (14, "Software", "EUR")
    assert row.mode is FulfillmentMode.MANUAL
    assert row.warranty_days == 30
    assert row.is_active is False


def test_bad_price_is_a_row_error_not_a_crash() -> None:
    """One bad row must not cost the admin the other 999."""
    result = parse_csv(f"{HEADER}\n,Good,,1.00,,,,,,\n,Bad,,abc,,,,,,")

    assert len(result.rows) == 1
    assert result.rows[0].name == "Good"
    assert len(result.errors) == 1
    assert "Line 3" in result.errors[0]
    assert "abc" in result.errors[0]


def test_missing_name_is_a_row_error() -> None:
    result = parse_csv(f"{HEADER}\n,,,1.00,,,,,,")

    assert result.rows == []
    assert "name" in result.errors[0].lower()


def test_unknown_mode_is_rejected_not_coerced() -> None:
    """Silently defaulting a typo'd mode to auto would ship instant delivery on a product the
    admin meant to fulfil by hand."""
    result = parse_csv(f"{HEADER}\n,Kiro,,1.00,,atuo,,,,")

    assert result.rows == []
    assert "atuo" in result.errors[0]


def test_missing_required_column_aborts_the_whole_file() -> None:
    with pytest.raises(ValueError, match="price"):
        parse_csv("id,name,category\n,Kiro,Software")


def test_row_cap_is_enforced() -> None:
    body = "\n".join(f",Product {i},,1.00,,,,,," for i in range(MAX_ROWS + 1))
    with pytest.raises(ValueError, match="rows"):
        parse_csv(f"{HEADER}\n{body}")


def test_excel_bom_and_semicolons_are_tolerated() -> None:
    """Excel writes a BOM, and a European locale writes semicolons. Both are what an admin
    actually uploads."""
    text = "﻿" + HEADER.replace(",", ";") + "\n;Kiro Pro;;9.99;;;;;;"

    result = parse_csv(text)

    assert result.errors == []
    assert result.rows[0].name == "Kiro Pro"
