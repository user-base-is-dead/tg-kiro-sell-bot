"""A crypto invoice opened from a product knows it is a purchase, not a wallet top-up.

Mechanically the two are the same — both credit the wallet. But a buyer who pressed "Pay with
Crypto" on a product was shown a screen headed "Top-Up Amount", and pressing Cancel dropped them on
the generic Top Up Wallet menu with no trace of what they had been buying.
"""

from __future__ import annotations

import pytest

from app.bot.handlers.payments import topup_crypto
from app.database.models.catalog import FulfillmentMode
from app.database.models.crypto import CryptoPayment
from app.database.repositories.user_repo import UserRepo
from app.services.catalog_service import add_stock, create_product


class _FakeQueryMessage:
    def __init__(self) -> None:
        self.text: str | None = None

    async def edit_text(self, text: str, reply_markup=None):  # noqa: ANN001
        self.text = text
        return self


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeQueryMessage()

    async def answer(self, text: str = "", show_alert: bool = False):  # noqa: FBT001,FBT002
        return None


async def _user_and_product(session, telegram_id: int) -> tuple[int, int]:
    user, _ = await UserRepo(session).upsert_from_telegram(
        telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
        chat_id=telegram_id, default_locale="en",
    )
    product_id = await create_product(
        session, category_id=None, name="Kiro Pro Max", description=None, price_minor=510,
        currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=3,
        delivery_info=None, image_file_id=None,
    )
    await add_stock(
        session, product_id=product_id, plaintext_payloads=["key-1"], added_by_admin_id=1
    )
    return user.id, product_id


@pytest.mark.asyncio
async def test_a_purchase_invoice_does_not_call_itself_a_top_up(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        user_id, product_id = await _user_and_product(session, 4301)
        text, _ = await topup_crypto.render_payment_details(
            session, user_id, 5.10, "en", purchase_product_id=product_id
        )

    assert "Top-Up Amount" not in text
    assert "Order Amount" in text


@pytest.mark.asyncio
async def test_a_plain_top_up_invoice_is_unchanged(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        user_id, _ = await _user_and_product(session, 4302)
        text, _ = await topup_crypto.render_payment_details(session, user_id, 15.0, "en")

    assert "Top-Up Amount" in text


@pytest.mark.asyncio
async def test_cancelling_a_purchase_invoice_returns_to_the_product(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        user_id, product_id = await _user_and_product(session, 4303)
        await topup_crypto.render_payment_details(
            session, user_id, 5.10, "en", purchase_product_id=product_id
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        payment = (await session.execute(__import__("sqlalchemy").select(CryptoPayment))).scalars().one()
        query = _FakeQuery(f"cancel_topup_crypto:{payment.id}")
        await topup_crypto.on_cancel_topup_payment(query, session, user)

        assert payment.status == "CANCELLED", "the live invoice must still be closed"
        assert "KIRO PRO MAX" in (query.message.text or ""), (
            "Cancel stranded the buyer on the Top Up Wallet screen"
        )
        assert "Top Up Wallet" not in (query.message.text or "")
