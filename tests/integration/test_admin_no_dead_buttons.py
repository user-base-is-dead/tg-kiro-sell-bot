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

from app.bot.handlers.admin import products
from app.core.config import get_settings
from app.database.models.catalog import FulfillmentMode
from app.services.catalog_service import create_product

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


def _tap(data: str, bot) -> Update:
    tg = TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin")
    message = Message(
        message_id=10,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=tg,
        text="screen",
    ).as_(bot)
    return Update(
        update_id=1,
        callback_query=CallbackQuery(id="1", from_user=tg, chat_instance="x", message=message, data=data),
    )


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# Stands in for the User row the middleware would inject. Carries the fields the handlers under
# audit actually read — nav:home renders a welcome line from first_name.
ADMIN = SimpleNamespace(
    id=1, telegram_id=ADMIN_ID, locale="en", first_name="Admin", username="admin", chat_id=ADMIN_ID
)


async def test_every_button_on_the_product_list_routes_somewhere(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    """A button that looks tappable and silently does nothing is indistinguishable from a broken
    bot. Every callback the list draws has to reach a handler."""
    async with sqlite_sessionmaker() as session:
        await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=999,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        _text, markup = await products._render_list(session, 1)

        dead = []
        for data in _callbacks(markup):
            await ctx.clear()
            result = await dispatcher.feed_update(bot, _tap(data, bot), session=session, user=ADMIN)
            if result is UNHANDLED:
                dead.append(data)

        assert not dead, f"dead buttons on the product list: {dead}"


async def test_every_button_on_the_product_detail_routes_somewhere(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=999,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        rendered = await products._render_detail(session, product_id)
        assert rendered is not None
        _text, markup = rendered

        dead = []
        for data in _callbacks(markup):
            await ctx.clear()
            result = await dispatcher.feed_update(bot, _tap(data, bot), session=session, user=ADMIN)
            if result is UNHANDLED:
                dead.append(data)

        assert not dead, f"dead buttons on the product detail screen: {dead}"


async def test_every_edit_field_button_routes_somewhere(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    """_EDIT_FIELDS is a dict, so a code added there without a matching prompt branch would render
    a button that answers "Unknown field" — or nothing at all."""
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=999,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        dead = []
        for code in products._EDIT_FIELDS:
            await ctx.clear()
            result = await dispatcher.feed_update(
                bot, _tap(f"pedit:{code}:{product_id}", bot), session=session, user=ADMIN
            )
            if result is UNHANDLED:
                dead.append(code)

        assert not dead, f"edit fields with no prompt: {dead}"


async def test_every_button_on_the_admin_panel_routes_somewhere(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    """The panel is the top-level screen. Removing the Payments button must not have left any of
    its neighbours pointing at nothing."""
    from app.bot.handlers.admin.panel import _panel_keyboard

    async with sqlite_sessionmaker() as session:
        dead = []
        for data in _callbacks(_panel_keyboard("en")):
            await ctx.clear()
            result = await dispatcher.feed_update(bot, _tap(data, bot), session=session, user=ADMIN)
            if result is UNHANDLED:
                dead.append(data)

        assert not dead, f"dead buttons on the admin panel: {dead}"


async def test_every_category_wizard_button_routes_somewhere(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    from app.bot.handlers.admin import categories

    async with sqlite_sessionmaker() as session:
        dead = []
        for step in categories._CATEGORY_STEPS:
            for data in _callbacks(categories._cat_step_keyboard(step)):
                await ctx.clear()
                await ctx.set_state(categories._CAT_STEP_STATES[step])
                await ctx.update_data(name="Probe", emoji=None)
                result = await dispatcher.feed_update(
                    bot, _tap(data, bot), session=session, user=ADMIN
                )
                if result is UNHANDLED:
                    dead.append(f"{step}:{data}")

        assert not dead, f"dead category wizard buttons: {dead}"


async def test_every_wizard_step_button_routes_somewhere(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    """Each step's own keyboard, checked while that step's state is active — a Skip or preset that
    routes nowhere would strand the admin mid-wizard with no way forward."""
    async with sqlite_sessionmaker() as session:
        dead = []
        for step in products._PRODUCT_STEPS:
            markup = products._step_keyboard(step)
            for data in _callbacks(markup):
                await ctx.clear()
                await ctx.set_state(products._STEP_STATES[step])
                # Enough collected data that a handler reaching the end can actually finish.
                await ctx.update_data(
                    category_id=None,
                    name="Probe",
                    description=None,
                    price_minor=100,
                    currency="USD",
                    fulfillment_mode=FulfillmentMode.AUTO,
                    warranty_days=0,
                    delivery_info=None,
                )
                result = await dispatcher.feed_update(
                    bot, _tap(data, bot), session=session, user=ADMIN
                )
                if result is UNHANDLED:
                    dead.append(f"{step}:{data}")

        assert not dead, f"dead wizard buttons: {dead}"
