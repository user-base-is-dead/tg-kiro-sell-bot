from __future__ import annotations

import pytest

from app.database.models.wallet import TxnType
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services import wallet_service
from app.utils.errors import UserError


async def _make_user(sessionmaker, telegram_id: int):
    async with sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=telegram_id, username=f"user{telegram_id}", first_name="T", last_name=None,
            chat_id=telegram_id, default_locale="en",
        )
        await session.commit()
        return user.id


@pytest.mark.asyncio
async def test_credit_then_debit_balance_matches_sum_of_transactions(sqlite_sessionmaker):
    user_id = await _make_user(sqlite_sessionmaker, 1001)

    async with sqlite_sessionmaker() as session:
        await wallet_service.credit(
            session, user_id=user_id, amount_minor=1000, currency="USD", type_=TxnType.TOPUP, idempotency_key="c1"
        )
        await wallet_service.debit(
            session, user_id=user_id, amount_minor=400, currency="USD", type_=TxnType.PURCHASE, idempotency_key="d1"
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        wallet = await WalletRepo(session).get_or_create(user_id, currency="USD")
        txns = await WalletRepo(session).list_transactions(wallet.id, limit=50)
        assert wallet.balance_minor == 600
        assert sum(t.amount_minor for t in txns) == wallet.balance_minor


@pytest.mark.asyncio
async def test_debit_beyond_balance_raises_and_does_not_mutate(sqlite_sessionmaker):
    user_id = await _make_user(sqlite_sessionmaker, 1002)

    async with sqlite_sessionmaker() as session:
        await wallet_service.credit(
            session, user_id=user_id, amount_minor=100, currency="USD", type_=TxnType.TOPUP, idempotency_key="c2"
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        with pytest.raises(UserError):
            await wallet_service.debit(
                session, user_id=user_id, amount_minor=999999, currency="USD", type_=TxnType.PURCHASE, idempotency_key="d2"
            )
        await session.rollback()

    async with sqlite_sessionmaker() as session:
        wallet = await WalletRepo(session).get_or_create(user_id, currency="USD")
        assert wallet.balance_minor == 100  # unchanged


@pytest.mark.asyncio
async def test_double_click_same_idempotency_key_only_applies_once(sqlite_sessionmaker):
    user_id = await _make_user(sqlite_sessionmaker, 1003)

    async with sqlite_sessionmaker() as session:
        t1 = await wallet_service.credit(
            session, user_id=user_id, amount_minor=500, currency="USD", type_=TxnType.TOPUP, idempotency_key="same-key"
        )
        t2 = await wallet_service.credit(
            session, user_id=user_id, amount_minor=500, currency="USD", type_=TxnType.TOPUP, idempotency_key="same-key"
        )
        await session.commit()
        assert t1.id == t2.id

    async with sqlite_sessionmaker() as session:
        wallet = await WalletRepo(session).get_or_create(user_id, currency="USD")
        assert wallet.balance_minor == 500  # not 1000 — the replay was a no-op
