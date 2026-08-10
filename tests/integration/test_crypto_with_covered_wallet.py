"""Crypto stays available even when the wallet already covers the price.

Crypto used to be refused outright on a covered wallet — a "$0.00 invoice would be a dead end"
popup. But a balance is often being saved on purpose, and a buyer who picks crypto over their own
funds is telling us exactly that. They now get an invoice for the FULL price, so the top-up and
the purchase cancel out and the saved balance survives the order.
"""

from __future__ import annotations

import pytest

from app.bot.callbacks import OrderCB
from app.bot.handlers.orders import checkout
from app.bot.handlers.payments import topup_crypto
from app.database.models.catalog import FulfillmentMode
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services.catalog_service import add_stock, create_product


class _FakeQueryMessage:
    def __init__(self) -> None:
        self.text: str | None = None

    async def edit_text(self, text: str, reply_markup=None):  # noqa: ANN001 - test double
        self.text = text
        return self


class _FakeQuery:
    def __init__(self) -> None:
        self.message = _FakeQueryMessage()
        self.alerts: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False):  # noqa: FBT001,FBT002
        if text:
            self.alerts.append(text)


@pytest.mark.asyncio
async def test_a_covered_wallet_can_still_pay_the_full_price_in_crypto(
    sqlite_sessionmaker, monkeypatch
) -> None:
    async with sqlite_sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=4101, username="u4101", first_name="T", last_name=None,
            chat_id=4101, default_locale="en",
        )
        user_id = user.id
        product_id = await create_product(
            session, category_id=None, name="Kiro Pro", description=None, price_minor=510,
            currency="USD", fulfillment_mode=FulfillmentMode.AUTO, warranty_days=1,
            delivery_info=None, image_file_id=None,
        )
        await add_stock(
            session, product_id=product_id, plaintext_payloads=["key-1"], added_by_admin_id=1
        )
        wallet = await WalletRepo(session).get_or_create(user_id, currency="USD")
        wallet.balance_minor = 100_000  # $1000 saved up, far more than the $5.10 price
        await session.commit()

    invoiced: list[float] = []

    async def _fake_details(session, uid, amount, locale, **kwargs):  # noqa: ANN001 - test double
        invoiced.append(amount)
        return "invoice", None

    monkeypatch.setattr(topup_crypto, "render_payment_details", _fake_details)

    async with sqlite_sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        query = _FakeQuery()
        await checkout.on_pay_with_crypto(
            query, OrderCB(action="crypto", product_id=str(product_id)), session, user
        )

    assert not query.alerts, f"crypto was refused: {query.alerts}"
    assert invoiced == [5.10], "the invoice must be the full price, not a $0.00 dead end"
    assert query.message.text is not None, "the invoice screen was never shown"
