from __future__ import annotations

from app.bot.callbacks import OrderCB
from app.bot.handlers.orders.checkout import render_payment_choice
from app.database.models.catalog import FulfillmentMode
from app.database.repositories.user_repo import UserRepo
from app.services.catalog_service import add_stock, create_product


async def _make_user(sessionmaker, telegram_id: int) -> int:
    async with sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
            chat_id=telegram_id, default_locale="en",
        )
        await session.commit()
        return user.id


async def _product(session, *, price_minor: int) -> int:
    product_id = await create_product(
        session, category_id=None, name="Kiro Pro", description=None, price_minor=price_minor,
        currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=1,
        delivery_info=None, image_file_id=None,
    )
    await add_stock(
        session, product_id=product_id, plaintext_payloads=["key-1"], added_by_admin_id=1
    )
    return product_id


def _targets(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_buy_now_offers_both_wallet_and_crypto(sqlite_sessionmaker) -> None:
    """Buy Now went straight to a wallet-only Confirm screen — crypto was never offered even
    though the bot's whole top-up path is crypto."""
    user_id = await _make_user(sqlite_sessionmaker, 4001)
    async with sqlite_sessionmaker() as session:
        product_id = await _product(session, price_minor=500)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        _, markup = await render_payment_choice(session, product_id, user)

    targets = _targets(markup)
    assert OrderCB(action="wallet", product_id=str(product_id)).pack() in targets
    assert OrderCB(action="crypto", product_id=str(product_id)).pack() in targets


async def test_a_short_wallet_says_how_much_is_missing(sqlite_sessionmaker) -> None:
    """The old flow let the buyer press Confirm on a balance that could not pay, and the only
    feedback was a generic "insufficient balance" popup after the fact."""
    user_id = await _make_user(sqlite_sessionmaker, 4002)
    async with sqlite_sessionmaker() as session:
        product_id = await _product(session, price_minor=500)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        text, _ = await render_payment_choice(session, product_id, user)

    # Empty wallet against a $5.00 product.
    assert "$5.00 short" in text


async def test_an_unavailable_product_has_no_payment_screen(sqlite_sessionmaker) -> None:
    """No stock means nothing to choose a payment method for; the caller shows the generic
    unknown-action alert on None rather than rendering a screen that cannot complete."""
    user_id = await _make_user(sqlite_sessionmaker, 4003)
    async with sqlite_sessionmaker() as session:
        product_id = await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=500,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=1,
            delivery_info=None, image_file_id=None,
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        assert await render_payment_choice(session, product_id, user) is None


async def test_the_choice_screen_takes_no_hold(sqlite_sessionmaker) -> None:
    """Merely *showing* the payment options reserves nothing. A credential is committed when a
    method is actually picked — a shopper who opens this screen and wanders off has taken nothing
    off the shelf."""
    from app.services import stock_hold_service

    user_id = await _make_user(sqlite_sessionmaker, 4004)
    async with sqlite_sessionmaker() as session:
        product_id = await _product(session, price_minor=500)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        await render_payment_choice(session, product_id, user)
        assert await stock_hold_service.held_count(session, product_id) == 0
