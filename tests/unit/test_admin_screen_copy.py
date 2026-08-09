from __future__ import annotations


async def test_product_screen_explains_what_the_buttons_do(monkeypatch) -> None:
    """The whole body used to be "5 products total." — the panel above it documents every button,
    and the screens below it documented nothing."""
    import app.bot.handlers.admin.products as products

    async def _fake_count(_session, **_kw):
        return 0

    monkeypatch.setattr(products, "_count_products", _fake_count)

    text, _ = await products._render_list(None, 1)

    assert "Add Product" in text
    assert "Import CSV" in text
    assert len(text) > 200, "a one-line screen is the defect being fixed"


async def test_product_screen_names_every_button_it_shows(monkeypatch) -> None:
    """A described-but-missing button reads as a broken screen, and a button nobody explained is
    the original complaint. The copy and the keyboard have to agree."""
    import app.bot.handlers.admin.products as products

    async def _fake_count(_session, **_kw):
        return 0

    monkeypatch.setattr(products, "_count_products", _fake_count)

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

    async def _fake_count(_session, **_kw):
        return 0

    monkeypatch.setattr(products, "_count_products", _fake_count)

    text, markup = await products._render_list(None, 1, name_like="kiro")
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert "kiro" in text
    assert any("kiro" in label for label in labels), "no way to see or clear the active filter"
