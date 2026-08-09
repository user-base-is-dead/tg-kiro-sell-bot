from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.handlers.user.start import cmd_start_with_ref
from app.database.repositories.user_repo import UserRepo


class _FakeMessage:
    """Just enough of aiogram's Message for the handler's welcome reply."""

    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


async def _make_user(sessionmaker, telegram_id: int):
    async with sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
            chat_id=telegram_id, default_locale="en",
        )
        await session.commit()
        return user.id, user.referral_code


async def _open_deep_link(sessionmaker, *, user_id: int, code: str, is_new_user: bool):
    async with sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        await cmd_start_with_ref(
            _FakeMessage(),  # type: ignore[arg-type]
            SimpleNamespace(args=f"ref_{code}"),  # type: ignore[arg-type]
            session,
            user,
            is_new_user,
        )
        await session.commit()

    async with sessionmaker() as session:
        return (await UserRepo(session).get_by_id(user_id)).referred_by_id


@pytest.mark.asyncio
async def test_new_user_gets_credited_to_referrer(sqlite_sessionmaker):
    referrer_id, code = await _make_user(sqlite_sessionmaker, 3001)
    referee_id, _ = await _make_user(sqlite_sessionmaker, 3002)

    referred_by = await _open_deep_link(sqlite_sessionmaker, user_id=referee_id, code=code, is_new_user=True)
    assert referred_by == referrer_id


@pytest.mark.asyncio
async def test_existing_user_cannot_use_a_referral_link(sqlite_sessionmaker):
    referrer_id, code = await _make_user(sqlite_sessionmaker, 3003)
    existing_id, _ = await _make_user(sqlite_sessionmaker, 3004)

    referred_by = await _open_deep_link(sqlite_sessionmaker, user_id=existing_id, code=code, is_new_user=False)
    assert referred_by is None


@pytest.mark.asyncio
async def test_referrer_cannot_be_changed_once_set(sqlite_sessionmaker):
    first_id, first_code = await _make_user(sqlite_sessionmaker, 3005)
    second_id, second_code = await _make_user(sqlite_sessionmaker, 3006)
    referee_id, _ = await _make_user(sqlite_sessionmaker, 3007)

    await _open_deep_link(sqlite_sessionmaker, user_id=referee_id, code=first_code, is_new_user=True)
    referred_by = await _open_deep_link(sqlite_sessionmaker, user_id=referee_id, code=second_code, is_new_user=True)
    assert referred_by == first_id
    assert referred_by != second_id


@pytest.mark.asyncio
async def test_self_referral_is_ignored(sqlite_sessionmaker):
    user_id, code = await _make_user(sqlite_sessionmaker, 3008)

    referred_by = await _open_deep_link(sqlite_sessionmaker, user_id=user_id, code=code, is_new_user=True)
    assert referred_by is None
