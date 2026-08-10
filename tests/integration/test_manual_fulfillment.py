"""Manual fulfilment: the admin gets told, and the auto stock pool is never touched.

Manual mode was effectively dead. No admin was pinged when an order landed, so the buyer got
"being prepared" and then silence for as long as nobody happened to open the admin panel. Worse,
checkout reserved a credential from the product's pool on the way through — stock the admin was
keeping for something else, taken for an order that would be fulfilled by hand — and a manual
product with an empty pool could not be bought at all, because the hold failed and the confirm
screen refused to render.
"""

from __future__ import annotations

import pytest

from app.bot.callbacks import OrderCB
from app.bot.handlers.orders import checkout
from app.database.models.catalog import FulfillmentMode
from app.database.models.order import OrderStatus
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services import order_service, stock_hold_service
from app.services.catalog_service import add_stock, create_product


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None):  # noqa: ANN001
        self.sent.append((chat_id, text))


class _FakeQueryMessage:
    def __init__(self) -> None:
        self.text: str | None = None

    async def edit_text(self, text: str, reply_markup=None):  # noqa: ANN001
        self.text = text
        return self


class _FakeQuery:
    def __init__(self, bot=None) -> None:  # noqa: ANN001
        self.message = _FakeQueryMessage()
        self.bot = bot or _FakeBot()
        self.alerts: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False):  # noqa: FBT001,FBT002
        if text:
            self.alerts.append(text)


async def _setup(session, *, telegram_id: int, stock: list[str], balance_minor: int) -> tuple[int, int]:
    user, _ = await UserRepo(session).upsert_from_telegram(
        telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
        chat_id=telegram_id, default_locale="en",
    )
    product_id = await create_product(
        session, category_id=None, name="Hand-made", description=None, price_minor=500,
        currency="USD", fulfillment_mode=FulfillmentMode.MANUAL, warranty_days=0,
        delivery_info=None, image_file_id=None,
    )
    if stock:
        await add_stock(
            session, product_id=product_id, plaintext_payloads=stock, added_by_admin_id=1
        )
    wallet = await WalletRepo(session).get_or_create(user.id, currency="USD")
    wallet.balance_minor = balance_minor
    return user.id, product_id


@pytest.mark.asyncio
async def test_a_manual_checkout_reserves_none_of_the_stock_pool(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        user_id, product_id = await _setup(
            session, telegram_id=4201, stock=["saved-1", "saved-2"], balance_minor=100_000
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        assert await checkout.render_checkout_confirm(session, product_id, user) is not None
        assert await stock_hold_service.held_count(session, product_id) == 0, "a credential was taken"
        assert await ProductRepo(session).available_stock_count(product_id) == 2


@pytest.mark.asyncio
async def test_a_manual_product_with_no_stock_is_still_buyable(sqlite_sessionmaker) -> None:
    """Manual products are not backed by a code pool at all — an empty one is the normal case."""
    async with sqlite_sessionmaker() as session:
        user_id, product_id = await _setup(
            session, telegram_id=4202, stock=[], balance_minor=100_000
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        rendered = await checkout.render_checkout_confirm(session, product_id, user)
        assert rendered is not None, "an empty pool blocked a hand-fulfilled purchase"


@pytest.mark.asyncio
async def test_placing_a_manual_order_pings_the_admins(sqlite_sessionmaker, monkeypatch) -> None:
    from app.core import config

    settings = config.get_settings()
    monkeypatch.setattr(type(settings), "admin_ids", property(lambda self: [777, 888]))

    async with sqlite_sessionmaker() as session:
        user_id, product_id = await _setup(
            session, telegram_id=4203, stock=["saved-1"], balance_minor=100_000
        )
        await session.commit()

    bot = _FakeBot()
    async with sqlite_sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        query = _FakeQuery(bot)
        await checkout.on_checkout_confirm(
            query, OrderCB(action="confirm", product_id=str(product_id)), session, user
        )
        await session.commit()

    assert [chat_id for chat_id, _ in bot.sent] == [777, 888], "admins were never told"
    assert "awaiting fulfilment" in bot.sent[0][1]

    async with sqlite_sessionmaker() as session:
        pending = await OrderRepo(session).list_pending_manual()
        assert len(pending) == 1
        assert pending[0].status is OrderStatus.PROCESSING
        # The order is hand-fulfilled, so the saved credential stays on the shelf for auto use.
        assert await ProductRepo(session).available_stock_count(product_id) == 1


@pytest.mark.asyncio
async def test_fulfilling_by_hand_leaves_the_saved_credential_alone(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        user_id, product_id = await _setup(
            session, telegram_id=4204, stock=["saved-1"], balance_minor=100_000
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        placed = await order_service.place_order(session, user_id=user_id, product_id=product_id)
        order_id = placed.order.id
        await session.commit()

    async with sqlite_sessionmaker() as session:
        await order_service.fulfill_manual_order(
            session, order_id=order_id, delivery_payload="typed by hand", admin_telegram_id=1
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await ProductRepo(session).available_stock_count(product_id) == 1, (
            "the admin's saved auto-mode credential was consumed by a manual delivery"
        )
