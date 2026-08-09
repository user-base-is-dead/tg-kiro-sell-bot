from __future__ import annotations

import pytest

from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo
from app.services.gift_service import create_gift_code, redeem_gift_code
from app.utils.errors import UserError


async def _make_user(sessionmaker, telegram_id: int):
    async with sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
            chat_id=telegram_id, default_locale="en",
        )
        await session.commit()
        return user.id


@pytest.mark.asyncio
async def test_redeem_credits_wallet_and_marks_exhausted_at_max_uses(sqlite_sessionmaker):
    async with sqlite_sessionmaker() as session:
        code = await create_gift_code(
            session, value_minor=500, currency="USD", max_uses=1, per_user_limit=1, expires_at=None, admin_id=999
        )
        await session.commit()

    user_id = await _make_user(sqlite_sessionmaker, 2001)

    async with sqlite_sessionmaker() as session:
        gift = await redeem_gift_code(session, user_id=user_id, code_plaintext=code)
        await session.commit()
        assert gift.used_count == 1
        assert gift.status.value == "EXHAUSTED"

    async with sqlite_sessionmaker() as session:
        wallet = await WalletRepo(session).get_or_create(user_id, currency="USD")
        assert wallet.balance_minor == 500


@pytest.mark.asyncio
async def test_exhausted_code_rejected_for_next_user(sqlite_sessionmaker):
    async with sqlite_sessionmaker() as session:
        code = await create_gift_code(
            session, value_minor=100, currency="USD", max_uses=1, per_user_limit=1, expires_at=None, admin_id=999
        )
        await session.commit()

    user1 = await _make_user(sqlite_sessionmaker, 2002)
    user2 = await _make_user(sqlite_sessionmaker, 2003)

    async with sqlite_sessionmaker() as session:
        await redeem_gift_code(session, user_id=user1, code_plaintext=code)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        with pytest.raises(UserError):
            await redeem_gift_code(session, user_id=user2, code_plaintext=code)


@pytest.mark.asyncio
async def test_invalid_code_rejected(sqlite_sessionmaker):
    user_id = await _make_user(sqlite_sessionmaker, 2004)
    async with sqlite_sessionmaker() as session:
        with pytest.raises(UserError):
            await redeem_gift_code(session, user_id=user_id, code_plaintext="NOT-A-REAL-CODE")
