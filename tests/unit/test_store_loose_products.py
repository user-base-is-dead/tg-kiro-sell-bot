from __future__ import annotations

from types import SimpleNamespace

from app.bot.keyboards.products import category_grid, product_detail


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_loose_products_render_above_the_folders() -> None:
    """Asked for explicitly: a product with no category belongs outside the folders, not inside a
    fake one."""
    loose = [SimpleNamespace(id=1, name="Kiro Pro", price_minor=999, currency="USD")]
    categories = [SimpleNamespace(id=5, name="Software", emoji="💾")]

    labels = _labels(category_grid(categories, "en", loose=loose))

    assert labels.index("📦 Kiro Pro — $9.99") < labels.index("💾 Software")


def test_no_divider_when_there_are_no_loose_products() -> None:
    labels = _labels(category_grid([SimpleNamespace(id=5, name="Software", emoji="💾")], "en", loose=[]))

    assert not any("─" in label for label in labels)


def test_product_detail_back_target_survives_a_null_category() -> None:
    """nav.py does int(target.removeprefix("cat-")), so a None category id would produce
    "cat-None" and raise ValueError the moment a buyer pressed Back."""
    product = SimpleNamespace(id=1, name="Kiro Pro", price_minor=999, currency="USD", category_id=None)
    view = SimpleNamespace(display_status=SimpleNamespace(value="IN_STOCK"), available_stock=3, product=product)

    targets = [
        b.callback_data
        for row in product_detail(product, view, "en", None).inline_keyboard
        for b in row
        if b.callback_data
    ]

    assert "nav:cat-None" not in targets
    assert "nav:categories" in targets
