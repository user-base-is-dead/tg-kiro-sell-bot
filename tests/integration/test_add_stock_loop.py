"""Add Stock is a loop, not a one-shot.

Stocking a product is naturally repetitive — keys arrive in batches, from different places, at
different times. The form used to hand the admin back to the product page after a single batch, so
adding a second one meant tapping `📦 Add Stock` again. Now the prompt comes back after every
message and `🔙 Back` is the only way out.
"""

from __future__ import annotations

import pytest

from app.bot.handlers.admin.products import _add_stock_screen, receive_stock
from app.bot.states.product_form import StockUploadForm
from app.database.models.catalog import (
    FulfillmentMode,
    Product,
    ProductStatus,
)
from app.database.repositories.product_repo import ProductRepo


class _FakeState:
    """Records whether the form state survived, which is what makes the loop a loop."""

    def __init__(self, data: dict) -> None:
        self._data = dict(data)
        self.state = StockUploadForm.payloads
        self.cleared = False

    async def get_data(self) -> dict:
        return self._data

    async def clear(self) -> None:
        self.cleared = True
        self.state = None


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        # A real Message always exposes both; the handler reads html_text so the admin's own
        # formatting survives into the buyer's copy.
        self.html_text = text
        self.replies: list[tuple[str, object]] = []

    async def answer(self, text: str, reply_markup=None):  # noqa: ANN001 - test double
        self.replies.append((text, reply_markup))
        return self


async def _make_product(session) -> int:
    product = Product(
        category_id=None,
        name="Kiro Pro",
        slug="kiro-pro",
        price_minor=500,
        currency="USD",
        status=ProductStatus.IN_STOCK,
        fulfillment_mode=FulfillmentMode.AUTO,
        warranty_days=1,
        is_active=True,
    )
    session.add(product)
    await session.flush()
    return product.id


@pytest.mark.asyncio
async def test_a_multi_line_credential_is_one_item(sqlite_sessionmaker):
    """A login is a login even when it spans four lines — splitting it would shred one account
    into several unusable fragments."""
    async with sqlite_sessionmaker() as session:
        product_id = await _make_product(session)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        state = _FakeState({"product_id": product_id})
        message = _FakeMessage("user@mail.com\nhunter2\n2FA: 123456")

        await receive_stock(message, state, session, _admin())
        await session.commit()

        assert await ProductRepo(session).available_stock_count(product_id) == 1
        assert not state.cleared, "the form must stay open so the next message adds more"

        text, markup = message.replies[-1]
        assert "Added <b>1</b> item(s)" in text, "the item is confirmed"
        assert "Add Stock" in text, "and the prompt is shown again"
        assert "In stock now: <b>1</b>" in text, "with a running total"
        back = [b for row in markup.inline_keyboard for b in row]
        assert any("Back" in b.text for b in back), "Back is the way out"


@pytest.mark.asyncio
async def test_successive_messages_keep_accumulating(sqlite_sessionmaker):
    """The point of the loop: message, message, message — no button in between."""
    async with sqlite_sessionmaker() as session:
        product_id = await _make_product(session)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        state = _FakeState({"product_id": product_id})

        await receive_stock(_FakeMessage("KEY-1"), state, session, _admin())
        await receive_stock(_FakeMessage("KEY-2"), state, session, _admin())
        last = _FakeMessage("login\npassword")
        await receive_stock(last, state, session, _admin())
        await session.commit()

        assert await ProductRepo(session).available_stock_count(product_id) == 3
        assert "In stock now: <b>3</b>" in last.replies[-1][0]
        assert not state.cleared


@pytest.mark.asyncio
async def test_an_empty_message_does_not_close_the_form(sqlite_sessionmaker):
    async with sqlite_sessionmaker() as session:
        product_id = await _make_product(session)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        state = _FakeState({"product_id": product_id})
        message = _FakeMessage("   \n  \n")

        await receive_stock(message, state, session, _admin())

        assert await ProductRepo(session).available_stock_count(product_id) == 0
        assert not state.cleared


@pytest.mark.asyncio
async def test_the_screen_names_the_product_it_is_stocking(sqlite_sessionmaker):
    """Two products' Add Stock screens look identical otherwise — the name is what tells them
    apart when the admin is going back and forth between them."""
    async with sqlite_sessionmaker() as session:
        product_id = await _make_product(session)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        text, _ = await _add_stock_screen(session, product_id)
        assert "Kiro Pro" in text


def _admin():
    class _Admin:
        telegram_id = 999

    return _Admin()
