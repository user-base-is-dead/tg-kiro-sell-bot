"""Pressing Buy Now on something that can't be bought right now opens the product page.

It used to answer "This action is no longer available" and do nothing else — a dead end, and a
misleading one: the product exists. The button reaching the most people is the one under a broadcast,
and a broadcast about a product is very often announcing that stock is *coming back*, so the reader
who taps it is exactly the person who should be looking at the page.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.bot.callbacks import ProductCB
from app.bot.handlers.products import browse
from app.database.models.catalog import Product, ProductStatus
from app.database.models.user import User


class FakeState:
    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}
        self.cleared = False

    async def set_state(self, state) -> None:
        self.state = state

    async def update_data(self, **kw) -> None:
        self.data.update(kw)

    async def clear(self) -> None:
        self.cleared = True
        self.state = None


class FakeMessage:
    """`editable` is False for the media broadcasts Telegram refuses to `edit_text`."""

    def __init__(self, *, editable: bool = True) -> None:
        self.editable = editable
        self.edited: list[str] = []
        self.answered: list[str] = []

    async def edit_text(self, text: str, reply_markup=None) -> None:
        if not self.editable:
            raise TelegramBadRequest(method=SimpleNamespace(), message="there is no text in the message to edit")
        self.edited.append(text)

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answered.append(text)


class FakeQuery:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message
        self.alerts: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.alerts.append((text, show_alert))


@pytest.fixture
def user() -> User:
    return User(id=1, telegram_id=5, username="u", referral_code="R", locale="en", chat_id=5)


async def _seed_product(sessionmaker, *, status: ProductStatus, stock: int) -> int:
    async with sessionmaker() as session:
        product = Product(
            name="Lovable Lite 1-year",
            slug=f"lovable-{status.value}",
            description="d",
            price_minor=1000,
            currency="USD",
            status=status,
            is_active=True,
            warranty_days=0,
            manual_stock=stock,
        )
        session.add(product)
        await session.commit()
        return product.id


async def test_out_of_stock_buy_now_lands_on_the_product_page(sqlite_sessionmaker, user) -> None:
    product_id = await _seed_product(sqlite_sessionmaker, status=ProductStatus.OUT_OF_STOCK, stock=0)
    message = FakeMessage()
    query = FakeQuery(message)

    async with sqlite_sessionmaker() as session:
        await browse.on_product(
            query,
            ProductCB(action="buy", id=str(product_id)),
            FakeState(),
            session,
            user,
        )

    # The page was shown, not an empty refusal.
    assert message.edited and "LOVABLE LITE 1-YEAR" in message.edited[0]
    # And they are told why the button didn't buy anything.
    assert query.alerts[-1][0] == "🔴 Sorry, this item just went out of stock."
    assert query.alerts[-1][1] is True


async def test_a_product_that_is_really_gone_still_says_so(sqlite_sessionmaker, user) -> None:
    """The one case where refusing is the truth: there is no page to open."""
    message = FakeMessage()
    query = FakeQuery(message)

    async with sqlite_sessionmaker() as session:
        await browse.on_product(query, ProductCB(action="buy", id="4242"), FakeState(), session, user)

    assert not message.edited
    assert query.alerts == [("This product is no longer available.", True)]


async def test_buy_now_under_a_photo_broadcast_sends_a_new_message(sqlite_sessionmaker, user) -> None:
    """A broadcast is whatever the admin composed, and `edit_text` on media is rejected outright —
    which surfaced as the generic error rather than a product page."""
    product_id = await _seed_product(sqlite_sessionmaker, status=ProductStatus.OUT_OF_STOCK, stock=0)
    message = FakeMessage(editable=False)
    query = FakeQuery(message)

    async with sqlite_sessionmaker() as session:
        await browse.on_product(
            query, ProductCB(action="buy", id=str(product_id)), FakeState(), session, user
        )

    assert not message.edited
    assert message.answered and "LOVABLE LITE 1-YEAR" in message.answered[0]


async def test_an_in_stock_buy_now_still_asks_how_many(sqlite_sessionmaker, user) -> None:
    """The regression guard for the fix: the normal path must not have been rerouted to the page."""
    product_id = await _seed_product(sqlite_sessionmaker, status=ProductStatus.IN_STOCK, stock=5)
    message = FakeMessage()
    query = FakeQuery(message)
    state = FakeState()

    async with sqlite_sessionmaker() as session:
        await browse.on_product(query, ProductCB(action="buy", id=str(product_id)), state, session, user)

    assert message.edited and "How many?" in message.edited[0]
    assert state.data["product_id"] == product_id
    assert query.alerts == [("", False)]
