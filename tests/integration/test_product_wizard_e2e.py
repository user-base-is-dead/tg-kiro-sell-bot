from __future__ import annotations

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from app.core.config import get_settings
from app.database.models.catalog import FulfillmentMode, ProductStatus
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import compute_display_status

BOT_ID = 99
ADMIN_ID = get_settings().admin_ids[0]


@pytest.fixture
def bot():
    fake = AsyncMock()
    fake.id = BOT_ID
    return fake


@pytest.fixture
async def ctx(dispatcher: Dispatcher):
    context = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=BOT_ID, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )
    await context.clear()
    yield context
    await context.clear()


def _tg_user() -> TgUser:
    return TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin")


def _message(text: str, bot) -> Message:
    return Message(
        message_id=10,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=_tg_user(),
        text=text,
    ).as_(bot)


def _tap(data: str, bot) -> Update:
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="1", from_user=_tg_user(), chat_instance="x", message=_message("wizard", bot), data=data
        ),
    )


def _type(text: str, bot) -> Update:
    return Update(update_id=1, message=_message(text, bot))


async def test_the_whole_wizard_can_be_driven_to_a_live_product(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    """The complaint was that creating a product meant typing free text at every step and then
    making a second trip to add stock — which crashed. This walks the wizard the way an admin now
    does: taps for every closed choice, typing only for the name, price and keys."""
    admin = SimpleNamespace(telegram_id=ADMIN_ID, locale="en")

    async with sqlite_sessionmaker() as session:
        async def feed(update):
            result = await dispatcher.feed_update(bot, update, session=session, user=admin)
            assert result is not UNHANDLED, f"nothing handled {update!r}"
            return result

        await feed(_tap("aprod:add::1", bot))
        assert await ctx.get_state() is not None, "the wizard did not open"

        await feed(_tap("pickcat:none", bot))          # tap: no category
        await feed(_type("Kiro Pro", bot))              # type: name
        await feed(_tap("pskip:description", bot))      # tap: skip description
        await feed(_type("9.99", bot))                  # type: price
        await feed(_tap("pmode:auto", bot))             # tap: auto, not typed "auto"
        await feed(_tap("pwar:30", bot))                # tap: warranty preset
        await feed(_tap("pskip:delivery_info", bot))    # tap: skip delivery info
        await feed(_type("KEY-1", bot))                 # type: the first licence key
        await session.commit()

    assert await ctx.get_state() is None, "the wizard did not close"

    async with sqlite_sessionmaker() as session:
        products = await ProductRepo(session).list_uncategorized(active_only=False)
        assert len(products) == 1
        product = products[0]

        assert product.name == "Kiro Pro"
        assert product.price_minor == 999
        assert product.category_id is None, "no fabricated Uncategorized folder"
        assert product.fulfillment_mode is FulfillmentMode.AUTO
        assert product.warranty_days == 30

        view = await compute_display_status(session, product)
        assert view.available_stock == 1
        assert view.display_status is not ProductStatus.OUT_OF_STOCK, "born dead again"


async def test_a_manual_product_never_reaches_the_stock_step(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    """MANUAL products have no stock pool, so the wizard finishes at delivery info instead of
    asking for keys that cannot exist."""
    admin = SimpleNamespace(telegram_id=ADMIN_ID, locale="en")

    async with sqlite_sessionmaker() as session:
        async def feed(update):
            return await dispatcher.feed_update(bot, update, session=session, user=admin)

        await feed(_tap("aprod:add::1", bot))
        await feed(_tap("pickcat:none", bot))
        await feed(_type("Hand Fulfilled", bot))
        await feed(_tap("pskip:description", bot))
        await feed(_type("19.99", bot))
        await feed(_tap("pmode:manual", bot))
        await feed(_tap("pwar:0", bot))
        await feed(_tap("pskip:delivery_info", bot))
        await session.commit()

    assert await ctx.get_state() is None, "MANUAL should finish, not wait for stock"

    async with sqlite_sessionmaker() as session:
        products = await ProductRepo(session).list_uncategorized(active_only=False)
        assert len(products) == 1
        view = await compute_display_status(session, products[0])
        assert view.display_status is ProductStatus.IN_STOCK


async def test_back_from_price_returns_to_description_without_losing_the_name(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    """Back mid-wizard against a real session — the unit test for this stubs the database out."""
    from app.bot.states.product_form import ProductForm

    admin = SimpleNamespace(telegram_id=ADMIN_ID, locale="en")

    async with sqlite_sessionmaker() as session:
        async def feed(update):
            return await dispatcher.feed_update(bot, update, session=session, user=admin)

        await feed(_tap("aprod:add::1", bot))
        await feed(_tap("pickcat:none", bot))
        await feed(_type("Kiro Pro", bot))
        await feed(_tap("pskip:description", bot))
        assert await ctx.get_state() == ProductForm.price

        await feed(_tap("pback:price", bot))

        assert await ctx.get_state() == ProductForm.description
        assert (await ctx.get_data())["name"] == "Kiro Pro"
