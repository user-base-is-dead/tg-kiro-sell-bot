from __future__ import annotations


def _stub_queries(monkeypatch, products) -> None:
    """Render the products screen with no database behind it.

    Every query it makes goes through a module-level seam for exactly this, so a screen-copy test
    can assert on wording without standing up a catalog.
    """

    async def _zero(_session, *_a, **_kw):
        return 0

    async def _none(_session, *_a, **_kw):
        return []

    for name in ("_count_products", "_count_loose", "_count_in_category"):
        monkeypatch.setattr(products, name, _zero)
    for name in ("_list_page", "_list_loose", "_list_categories", "_list_in_category"):
        monkeypatch.setattr(products, name, _none)


async def test_product_screen_explains_what_the_buttons_do(monkeypatch) -> None:
    """The whole body used to be "5 products total." — the panel above it documents every button,
    and the screens below it documented nothing."""
    import app.bot.handlers.admin.products as products

    _stub_queries(monkeypatch, products)

    text, _ = await products._render_list(None, 1)

    assert "Add Product" in text
    assert "Import CSV" in text
    assert len(text) > 200, "a one-line screen is the defect being fixed"


async def test_product_screen_names_every_button_it_shows(monkeypatch) -> None:
    """A described-but-missing button reads as a broken screen, and a button nobody explained is
    the original complaint. The copy and the keyboard have to agree."""
    import app.bot.handlers.admin.products as products

    _stub_queries(monkeypatch, products)

    text, markup = await products._render_list(None, 1)
    labels = [b.text for row in markup.inline_keyboard for b in row]

    for label in labels:
        if label.startswith(("➕", "📥", "📤", "🔍")):
            # Strip the emoji and compare on the words.
            words = label.split(" ", 1)[1]
            assert words in text, f"{label!r} is on screen but never explained"


async def test_search_filter_is_visible_when_active(monkeypatch) -> None:
    """A filtered list that looks identical to an unfiltered one is how an admin concludes their
    products have vanished."""
    import app.bot.handlers.admin.products as products

    _stub_queries(monkeypatch, products)

    text, markup = await products._render_list(None, 1, name_like="kiro")
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert "kiro" in text
    assert any("kiro" in label for label in labels), "no way to see or clear the active filter"
