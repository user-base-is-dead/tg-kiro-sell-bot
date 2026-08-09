from __future__ import annotations

import datetime
from datetime import UTC, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from app.bot.handlers.admin import users as admin_users
from app.core.config import get_settings
from app.database.models.user import User, UserStatus
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.wallet_repo import WalletRepo

BOT_ID = 99
ADMIN_ID = get_settings().admin_ids[0]

ADMIN = SimpleNamespace(
    id=1, telegram_id=ADMIN_ID, locale="en", first_name="Admin", username="admin", chat_id=ADMIN_ID
)


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


def _tg() -> TgUser:
    return TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin")


def _msg(text: str, bot) -> Message:
    return Message(
        message_id=10,
        date=datetime.datetime.now(UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=_tg(),
        text=text,
    ).as_(bot)


def _tap(data: str, bot) -> Update:
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="1", from_user=_tg(), chat_instance="x", message=_msg("screen", bot), data=data
        ),
    )


def _type(text: str, bot) -> Update:
    return Update(update_id=1, message=_msg(text, bot))


async def _seed(sessionmaker, count: int) -> None:
    base = datetime.datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    async with sessionmaker() as session:
        for i in range(count):
            session.add(
                User(
                    telegram_id=1000 + i,
                    username=f"user{i:03d}",
                    first_name=f"User {i}",
                    referral_code=f"REF{i:05d}",
                    first_seen_at=base + timedelta(days=i),
                    last_seen_at=base + timedelta(days=i),
                )
            )
        await session.commit()


async def test_the_list_shows_handle_id_and_join_time_for_each_member(sqlite_sessionmaker) -> None:
    """All three were asked for: username, user id, and the date/time they joined."""
    await _seed(sqlite_sessionmaker, 3)

    async with sqlite_sessionmaker() as session:
        text, _markup = await admin_users._render_list(session, 1)

    assert "@user000" in text
    assert "1000" in text, "the Telegram ID has to be on screen"
    assert "01 Jan 2026, 09:30" in text, "join date and time"
    assert "3</b> member(s) joined" in text


async def test_the_list_is_numbered_oldest_at_the_top(sqlite_sessionmaker) -> None:
    await _seed(sqlite_sessionmaker, 3)

    async with sqlite_sessionmaker() as session:
        text, _markup = await admin_users._render_list(session, 1)

    assert text.index("@user000") < text.index("@user001") < text.index("@user002")
    assert "<b>1.</b>" in text and "<b>3.</b>" in text


async def test_numbering_continues_across_pages(sqlite_sessionmaker) -> None:
    """Page 2 starts at 21, so a number is the member's signup rank rather than a row index."""
    await _seed(sqlite_sessionmaker, 25)

    async with sqlite_sessionmaker() as session:
        page_two, markup = await admin_users._render_list(session, 2)

    assert "<b>21.</b>" in page_two
    assert "Showing 21–25" in page_two
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "21" in labels and "25" in labels


async def test_paging_appears_only_past_twenty_and_walks_both_ways(sqlite_sessionmaker) -> None:
    await _seed(sqlite_sessionmaker, 45)

    async with sqlite_sessionmaker() as session:
        _t1, first = await admin_users._render_list(session, 1)
        _t2, second = await admin_users._render_list(session, 2)
        _t3, third = await admin_users._render_list(session, 3)

    def targets(m):
        return [b.callback_data for row in m.inline_keyboard for b in row if b.callback_data]

    assert "auser:list::2" in targets(first), "page 1 needs Next"
    assert not any(t == "auser:list::0" for t in targets(first)), "page 1 must not offer Previous"

    assert "auser:list::1" in targets(second)
    assert "auser:list::3" in targets(second)

    assert "auser:list::2" in targets(third)
    assert "auser:list::4" not in targets(third), "the last page must not offer Next"


async def test_twenty_per_page_exactly(sqlite_sessionmaker) -> None:
    await _seed(sqlite_sessionmaker, 45)

    async with sqlite_sessionmaker() as session:
        text, markup = await admin_users._render_list(session, 1)

    assert "Showing 1–20" in text
    numbered = [
        b.text for row in markup.inline_keyboard for b in row if b.text.strip("🚫").isdigit()
    ]
    assert len(numbered) == 20


async def test_an_empty_install_says_so_instead_of_rendering_a_blank_list(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        text, markup = await admin_users._render_list(session, 1)

    assert "Nobody has messaged the bot yet" in text
    targets = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert "nav:admin_panel" in targets, "still not a dead end"


async def test_the_profile_shows_the_deep_detail(sqlite_sessionmaker) -> None:
    """"dekh sakta hu deeply unke profile ko" — the old profile had six lines and no wallet control."""
    await _seed(sqlite_sessionmaker, 1)

    async with sqlite_sessionmaker() as session:
        target = (await UserRepo(session).list_page(offset=0, limit=1))[0]
        text = await admin_users._render_detail(session, target)

    for expected in (
        "@user000",
        "1000",                 # telegram id
        "01 Jan 2026, 09:30",   # joined, with time
        "Last seen",
        "Wallet",
        "Orders",
        "Referral code",
        "REF00000",
        "ACTIVE",
        "Language",
    ):
        assert expected in text, f"the profile does not show {expected!r}"


async def test_the_profile_offers_a_wallet_control_and_keeps_your_page(sqlite_sessionmaker) -> None:
    await _seed(sqlite_sessionmaker, 1)

    async with sqlite_sessionmaker() as session:
        target = (await UserRepo(session).list_page(offset=0, limit=1))[0]
        markup = admin_users._detail_keyboard(target, page=3)

    targets = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert f"auser:credit:{target.id}:3" in targets, "no way to give points from the profile"
    assert f"auser:ban:{target.id}:3" in targets
    assert "auser:list::3" in targets, "Back must return to page 3, not page 1"


async def test_crediting_a_user_from_the_profile_moves_real_money(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    """"click karke manually point vhi de sakta hu" — driven through the real dispatcher."""
    await _seed(sqlite_sessionmaker, 1)

    async with sqlite_sessionmaker() as session:
        target = (await UserRepo(session).list_page(offset=0, limit=1))[0]
        target_id = target.id

        assert await dispatcher.feed_update(
            bot, _tap(f"auser:credit:{target_id}:1", bot), session=session, user=ADMIN
        ) is not UNHANDLED
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await dispatcher.feed_update(
            bot, _type("+12.50 goodwill for the delay", bot), session=session, user=ADMIN
        ) is not UNHANDLED
        await session.commit()

    assert await ctx.get_state() is None, "the form should close after a successful adjustment"

    async with sqlite_sessionmaker() as session:
        wallet = await WalletRepo(session).get_or_create(
            target_id, currency=get_settings().default_currency
        )
        assert wallet.balance_minor == 1250


async def test_a_debit_below_zero_is_refused_and_changes_nothing(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    await _seed(sqlite_sessionmaker, 1)

    async with sqlite_sessionmaker() as session:
        target_id = (await UserRepo(session).list_page(offset=0, limit=1))[0].id
        await dispatcher.feed_update(
            bot, _tap(f"auser:credit:{target_id}:1", bot), session=session, user=ADMIN
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        await dispatcher.feed_update(bot, _type("-5", bot), session=session, user=ADMIN)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        wallet = await WalletRepo(session).get_or_create(
            target_id, currency=get_settings().default_currency
        )
        assert wallet.balance_minor == 0, "a refused debit must not move the balance"


async def test_an_unsigned_amount_is_refused_as_ambiguous(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    """"10" could mean credit or debit. Guessing wrong moves real money the wrong way, so the
    handler holds the form open and asks for a sign instead."""
    await _seed(sqlite_sessionmaker, 1)
    from app.bot.states.user_search_form import UserBalanceForm

    async with sqlite_sessionmaker() as session:
        target_id = (await UserRepo(session).list_page(offset=0, limit=1))[0].id
        await dispatcher.feed_update(
            bot, _tap(f"auser:credit:{target_id}:1", bot), session=session, user=ADMIN
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        await dispatcher.feed_update(bot, _type("10", bot), session=session, user=ADMIN)
        await session.commit()

    assert await ctx.get_state() == UserBalanceForm.amount, "the form must stay open to retry"

    async with sqlite_sessionmaker() as session:
        wallet = await WalletRepo(session).get_or_create(
            target_id, currency=get_settings().default_currency
        )
        assert wallet.balance_minor == 0


async def test_banning_from_the_profile_persists_and_flags_the_list(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    await _seed(sqlite_sessionmaker, 2)

    async with sqlite_sessionmaker() as session:
        target_id = (await UserRepo(session).list_page(offset=0, limit=2))[0].id
        assert await dispatcher.feed_update(
            bot, _tap(f"auser:ban:{target_id}:1", bot), session=session, user=ADMIN
        ) is not UNHANDLED
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert (await UserRepo(session).get_by_id(target_id)).status is UserStatus.BANNED
        text, markup = await admin_users._render_list(session, 1)
        assert "🚫" in text
        assert any("🚫" in b.text for row in markup.inline_keyboard for b in row)


async def test_every_button_on_the_user_list_and_profile_routes_somewhere(
    dispatcher: Dispatcher, sqlite_sessionmaker, bot, ctx
) -> None:
    await _seed(sqlite_sessionmaker, 25)

    async with sqlite_sessionmaker() as session:
        _text, list_markup = await admin_users._render_list(session, 2)
        target = (await UserRepo(session).list_page(offset=0, limit=1))[0]
        detail_markup = admin_users._detail_keyboard(target, page=2)

        dead = []
        for markup in (list_markup, detail_markup):
            for data in [
                b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data
            ]:
                await ctx.clear()
                if await dispatcher.feed_update(
                    bot, _tap(data, bot), session=session, user=ADMIN
                ) is UNHANDLED:
                    dead.append(data)

        assert not dead, f"dead buttons on the users screens: {dead}"
