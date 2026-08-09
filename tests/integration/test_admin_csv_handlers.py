from __future__ import annotations

import datetime
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Chat, Document, Message, Update
from aiogram.types import User as TgUser

from app.core.config import get_settings
from app.database.models.catalog import FulfillmentMode
from app.database.repositories.product_repo import ProductRepo
from app.services.catalog_service import create_product

BOT_ID = 99
ADMIN_ID = get_settings().admin_ids[0]
HEADER = "id,name,category,price,currency,mode,warranty,description,delivery_info,active"

ADMIN = SimpleNamespace(
    id=1, telegram_id=ADMIN_ID, locale="en", first_name="Admin", username="admin", chat_id=ADMIN_ID
)


@pytest.fixture
async def ctx(dispatcher: Dispatcher):
    context = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=BOT_ID, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )
    await context.clear()
    yield context
    await context.clear()


def _bot_returning(csv_text: str):
    """message.bot.download returns a file-like object — that is the only bot call the import
    handler makes, so the rest of the bot can stay a plain mock."""
    bot = AsyncMock()
    bot.id = BOT_ID
    bot.download = AsyncMock(return_value=io.BytesIO(csv_text.encode("utf-8")))
    return bot


def _upload(csv_text: str, bot) -> Update:
    tg = TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin")
    message = Message(
        message_id=10,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=tg,
        document=Document(
            file_id="f", file_unique_id="u", file_name="products.csv",
            file_size=len(csv_text.encode("utf-8")),
        ),
    ).as_(bot)
    return Update(update_id=1, message=message)


async def test_uploading_a_csv_creates_the_products(
    dispatcher: Dispatcher, sqlite_sessionmaker, ctx
) -> None:
    """The whole point of the feature: 1000 products without running the wizard 1000 times."""
    from app.bot.states.product_form import ProductImportForm

    body = "\n".join(f",Product {i},,1.{i:02d},,,,,," for i in range(50))
    bot = _bot_returning(f"{HEADER}\n{body}")

    await ctx.set_state(ProductImportForm.document)

    async with sqlite_sessionmaker() as session:
        result = await dispatcher.feed_update(
            bot, _upload(f"{HEADER}\n{body}", bot), session=session, user=ADMIN
        )
        assert result is not UNHANDLED, "the CSV upload was not handled"
        await session.commit()

    assert await ctx.get_state() is None, "the import form should close after a successful run"

    async with sqlite_sessionmaker() as session:
        assert await ProductRepo(session).count_all() == 50


async def test_reuploading_an_edited_export_updates_instead_of_duplicating(
    dispatcher: Dispatcher, sqlite_sessionmaker, ctx
) -> None:
    """Export → edit in Excel → re-upload is the documented bulk-edit path. If it duplicated, the
    catalogue would double every time an admin fixed a price."""
    from app.bot.states.product_form import ProductImportForm
    from app.services.product_import import to_csv

    async with sqlite_sessionmaker() as session:
        await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=999,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=0,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        products = await ProductRepo(session).list_page(offset=0, limit=10)
        exported = to_csv(products, {})

    edited = exported.replace("9.99", "19.99")
    assert edited != exported, "the price edit did not apply to the export"

    bot = _bot_returning(edited)
    await ctx.set_state(ProductImportForm.document)

    async with sqlite_sessionmaker() as session:
        await dispatcher.feed_update(bot, _upload(edited, bot), session=session, user=ADMIN)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        rows = await ProductRepo(session).list_page(offset=0, limit=10)
        assert len(rows) == 1, "re-uploading an export duplicated the product"
        assert rows[0].price_minor == 1999


async def test_a_bad_header_is_refused_without_touching_the_catalogue(
    dispatcher: Dispatcher, sqlite_sessionmaker, ctx
) -> None:
    """A malformed header means the file is not what the admin thinks it is, so applying half of it
    would be worse than refusing."""
    from app.bot.states.product_form import ProductImportForm

    text = "id,name,category\n,Kiro,Software"
    bot = _bot_returning(text)
    await ctx.set_state(ProductImportForm.document)

    async with sqlite_sessionmaker() as session:
        result = await dispatcher.feed_update(bot, _upload(text, bot), session=session, user=ADMIN)
        assert result is not UNHANDLED
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await ProductRepo(session).count_all() == 0


async def test_typing_text_instead_of_attaching_a_file_gets_an_answer(
    dispatcher: Dispatcher, sqlite_sessionmaker, ctx
) -> None:
    """Without its own handler the typed message falls through to whatever matches next, and the
    admin gets an unrelated screen instead of being told to attach the file."""
    from app.bot.states.product_form import ProductImportForm

    bot = AsyncMock()
    bot.id = BOT_ID
    await ctx.set_state(ProductImportForm.document)

    tg = TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin")
    message = Message(
        message_id=11,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=tg,
        text="here are my products",
    ).as_(bot)

    async with sqlite_sessionmaker() as session:
        result = await dispatcher.feed_update(
            bot, Update(update_id=1, message=message), session=session, user=ADMIN
        )

    assert result is not UNHANDLED, "a typed reply while awaiting a file went unanswered"
