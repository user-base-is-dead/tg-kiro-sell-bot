from __future__ import annotations

from types import SimpleNamespace

from app.bot.handlers.admin.products import PAD, _EDIT_FIELDS, _detail_keyboard, _render_detail
from app.database.models.catalog import FulfillmentMode
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import create_product


def test_detail_screen_offers_edit() -> None:
    """The detail screen had Add Stock / Toggle / Delete / Back and no Edit at all — a price could
    not be changed without deleting and recreating the product."""
    product = SimpleNamespace(id=7, is_active=True)
    targets = [b.callback_data for row in _detail_keyboard(product).inline_keyboard for b in row]

    assert any(t.startswith("aprod:edit") for t in targets)


def test_every_edit_callback_fits_telegram_limit() -> None:
    """callback_data is capped at 64 bytes; a product id is unbounded in principle."""
    for code in _EDIT_FIELDS:
        data = f"pedit:{code}:{9_999_999_999}"
        assert len(data.encode("utf-8")) <= 64, f"{code} callback is too long"


def test_every_closed_set_edit_callback_fits_too() -> None:
    """The value-carrying callbacks are the longest ones — a category id rides along with them."""
    for code, value in (("md", "manual"), ("wr", "365"), ("ct", "9999999999")):
        data = f"pedset:{code}:{value}:{9_999_999_999}"
        assert len(data.encode("utf-8")) <= 64, f"{code} set-callback is too long"


def test_edit_field_codes_are_short_not_words() -> None:
    """Spelled-out field names plus an id would blow the 64-byte cap on long ids."""
    assert all(len(code) <= 2 for code in _EDIT_FIELDS)


async def test_editing_price_persists(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=999,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        product.price_minor = 1999
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert (await ProductRepo(session).get_by_id(product_id)).price_minor == 1999


async def test_detail_bubble_is_padded_wider_than_its_own_copy(sqlite_sessionmaker) -> None:
    """A bubble and its inline keyboard share one width and Telegram takes the wider side. Without
    padding the text is the narrower side, so "Category: Uncategorized" (23 chars) squeezed the whole
    button column. The name is kept short here so the title line cannot supply the width instead."""
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=999,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=1,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        text, _ = await _render_detail(session, product_id)

    lines = text.split("\n")
    pad_line, field_lines = lines[-1], lines[2:-1]

    # Wider than the longest real field line, which is the whole point.
    assert set(pad_line) == {"⠀"}, "last line should be padding and nothing else"
    assert len(pad_line) >= 24

    # ...but not so wide that the pad wraps and renders as an unexplained blank gap.
    assert len(pad_line) <= 35

    # The width comes from the pad, not from the copy having been rewritten wider.
    assert len(field_lines) == 7
    assert all("⠀" not in line for line in field_lines)
    for line in field_lines:
        assert len(line) <= 23, f"field line grew past the pad's reason to exist: {line!r}"


def test_pad_is_invisible_not_whitespace() -> None:
    """Ordinary spaces are trimmed from message text and would widen nothing, so the pad has to be a
    printable character that renders as nothing."""
    assert PAD and not PAD.isspace()
    assert PAD.strip() == PAD


async def test_a_product_can_be_moved_out_of_its_category(sqlite_sessionmaker) -> None:
    """Editing the category to "none" has to produce a real NULL, not a fabricated folder."""
    from app.services.catalog_service import create_category

    async with sqlite_sessionmaker() as session:
        category_id = await create_category(
            session, name="Software", emoji=None, description=None, image_file_id=None
        )
        product_id = await create_product(
            session, category_id=category_id, name="Kiro Pro", description=None, price_minor=999,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        product.category_id = None
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert (await ProductRepo(session).get_by_id(product_id)).category_id is None
        assert await ProductRepo(session).count_uncategorized(active_only=False) == 1
