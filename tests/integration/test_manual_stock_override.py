"""A hand-set stock count, and what it does to a purchase.

The rule being pinned down here is the one that is easy to get subtly wrong: the override decides
*how many* are for sale, and the credential pool decides *how* each of those is delivered. An
override of 3 against 1 credential is one auto-delivered sale followed by two that land in the
admin's pending-fulfilment queue — not a product that flips wholesale into manual mode, and not a
product that refuses the second buyer.
"""

from __future__ import annotations

import pytest

from app.database.models.catalog import FulfillmentMode, ProductStatus
from app.database.models.order import OrderStatus
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services import order_service
from app.services.catalog_service import add_stock, compute_display_status, create_product
from app.utils.errors import UserError


async def _setup(session, *, telegram_id: int, credentials: list[str], manual_stock: int | None):
    user, _ = await UserRepo(session).upsert_from_telegram(
        telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
        chat_id=telegram_id, default_locale="en",
    )
    product_id = await create_product(
        session, category_id=None, name=f"Kiro {telegram_id}", description=None, price_minor=500,
        currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
        delivery_info=None, image_file_id=None, manual_stock=manual_stock,
    )
    if credentials:
        await add_stock(
            session, product_id=product_id, plaintext_payloads=credentials, added_by_admin_id=1
        )
    wallet = await WalletRepo(session).get_or_create(user.id, currency="USD")
    wallet.balance_minor = 100_000
    return user.id, product_id


@pytest.mark.asyncio
async def test_the_override_is_what_shoppers_see_not_the_credential_count(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        _, product_id = await _setup(session, telegram_id=5101, credentials=["K1"], manual_stock=12)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        view = await compute_display_status(session, product)
        assert view.available_stock == 12, "the credential count overruled the admin"
        assert view.display_status is ProductStatus.IN_STOCK


@pytest.mark.asyncio
async def test_sales_beyond_the_credentials_fall_through_to_manual_fulfilment(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        user_id, product_id = await _setup(
            session, telegram_id=5102, credentials=["K1"], manual_stock=3
        )
        await session.commit()

    # First buyer: a credential is free, so it is handed over on the spot.
    async with sqlite_sessionmaker() as session:
        first = await order_service.place_order(session, user_id=user_id, product_id=product_id)
        assert first.delivered_payload == "K1"
        assert first.order.status is OrderStatus.COMPLETED
        await session.commit()

    # Second buyer: the shelf is empty but the admin promised three, so the sale still happens —
    # this is the one that used to be refused outright with "out of stock".
    async with sqlite_sessionmaker() as session:
        other, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=5199, username="u5199", first_name="T", last_name=None,
            chat_id=5199, default_locale="en",
        )
        wallet = await WalletRepo(session).get_or_create(other.id, currency="USD")
        wallet.balance_minor = 100_000
        second = await order_service.place_order(session, user_id=other.id, product_id=product_id)
        assert second.delivered_payload is None, "there was no credential left to deliver"
        assert second.order.status is OrderStatus.PROCESSING, "it must reach the admin's queue"
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        assert product.manual_stock == 1, "both sales must count against the hand-set number"


@pytest.mark.asyncio
async def test_an_exhausted_override_closes_the_product(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        user_id, product_id = await _setup(
            session, telegram_id=5103, credentials=["K1"], manual_stock=1
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        await order_service.place_order(session, user_id=user_id, product_id=product_id)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        with pytest.raises(UserError):
            await order_service.place_order(session, user_id=user_id, product_id=product_id)


@pytest.mark.asyncio
async def test_a_zero_override_refuses_buyers_even_with_credentials_on_the_shelf(sqlite_sessionmaker) -> None:
    """Setting 0 is how a product is closed without disabling it — the credentials stay put."""
    async with sqlite_sessionmaker() as session:
        user_id, product_id = await _setup(
            session, telegram_id=5104, credentials=["K1", "K2"], manual_stock=0
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        view = await compute_display_status(session, product)
        assert view.display_status is ProductStatus.OUT_OF_STOCK

        with pytest.raises(UserError):
            await order_service.place_order(session, user_id=user_id, product_id=product_id)
        assert await ProductRepo(session).available_stock_count(product_id) == 2


@pytest.mark.asyncio
async def test_a_manual_product_never_spends_a_credential(sqlite_sessionmaker) -> None:
    """Frozen means frozen: MANUAL mode sells through the hand-set count while every loaded
    credential stays exactly where it is, including when the count runs out."""
    async with sqlite_sessionmaker() as session:
        user_id, product_id = await _setup(
            session, telegram_id=5106, credentials=["K1", "K2"], manual_stock=1
        )
        product = await ProductRepo(session).get_by_id(product_id)
        product.fulfillment_mode = FulfillmentMode.MANUAL
        await session.commit()

    async with sqlite_sessionmaker() as session:
        placed = await order_service.place_order(session, user_id=user_id, product_id=product_id)
        assert placed.delivered_payload is None, "a frozen credential was handed to a buyer"
        assert placed.order.status is OrderStatus.PROCESSING
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        assert product.manual_stock == 0
        view = await compute_display_status(session, product)
        assert view.display_status is ProductStatus.OUT_OF_STOCK
        assert await ProductRepo(session).available_stock_count(product_id) == 2, "the pool moved"


def test_the_mode_button_shows_the_split_instead_of_plain_auto() -> None:
    """35 on sale against 34 credentials is one hand-fulfilled order waiting to happen. The button
    used to call that "Auto", which is where the confusion started."""
    from types import SimpleNamespace

    from app.bot.handlers.admin.products import _fulfillment_label

    product = SimpleNamespace(fulfillment_mode=FulfillmentMode.AUTO, manual_stock=35)
    assert _fulfillment_label(product, 34) == "🔀 Auto ×34 + Manual ×1"

    # At or below the pool everything is auto-delivered, so the plain label is the true one.
    assert _fulfillment_label(SimpleNamespace(fulfillment_mode=FulfillmentMode.AUTO, manual_stock=30), 34) == (
        "⚡ Fulfillment: Auto"
    )
    assert _fulfillment_label(SimpleNamespace(fulfillment_mode=FulfillmentMode.MANUAL, manual_stock=35), 34) == (
        "🙋 Fulfillment: Manual"
    )


@pytest.mark.asyncio
async def test_switching_back_to_auto_restores_the_credential_count(sqlite_sessionmaker) -> None:
    from app.bot.handlers.admin.products import apply_edit_button

    class _Msg:
        async def edit_text(self, text, reply_markup=None):  # noqa: ANN001
            self.text = text

    class _Query:
        def __init__(self, data: str) -> None:
            self.data = data
            self.message = _Msg()

        async def answer(self, text: str = "", show_alert: bool = False):  # noqa: FBT001,FBT002
            pass

    async with sqlite_sessionmaker() as session:
        _, product_id = await _setup(
            session, telegram_id=5107, credentials=["K1", "K2", "K3"], manual_stock=50
        )
        product = await ProductRepo(session).get_by_id(product_id)
        product.fulfillment_mode = FulfillmentMode.MANUAL
        await session.commit()

    async with sqlite_sessionmaker() as session:
        await apply_edit_button(_Query(f"pedset:md:auto:{product_id}"), session)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        product = await ProductRepo(session).get_by_id(product_id)
        assert product.manual_stock is None, "the hand-set number survived the switch to Auto"
        view = await compute_display_status(session, product)
        assert view.available_stock == 3, "Auto must count the credentials that are loaded"


@pytest.mark.asyncio
async def test_without_an_override_nothing_changes(sqlite_sessionmaker) -> None:
    """The whole feature is opt-in: an untouched product still counts its own credentials, and
    still refuses a buyer when they run out."""
    async with sqlite_sessionmaker() as session:
        user_id, product_id = await _setup(
            session, telegram_id=5105, credentials=["K1"], manual_stock=None
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        placed = await order_service.place_order(session, user_id=user_id, product_id=product_id)
        assert placed.delivered_payload == "K1"
        await session.commit()

    # A second buyer, not the same one: repeating a purchase is idempotent by design and would hand
    # back the first order instead of testing the empty shelf.
    async with sqlite_sessionmaker() as session:
        other, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=5195, username="u5195", first_name="T", last_name=None,
            chat_id=5195, default_locale="en",
        )
        wallet = await WalletRepo(session).get_or_create(other.id, currency="USD")
        wallet.balance_minor = 100_000
        with pytest.raises(UserError):
            await order_service.place_order(session, user_id=other.id, product_id=product_id)
