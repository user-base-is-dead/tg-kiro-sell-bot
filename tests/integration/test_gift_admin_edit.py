"""A gift code is editable after it is created, and its detail screen says so.

The screen used to be a read-only dump with a single Back button: a typo in the description, a
wrong per-user limit or an expiry set too short all meant disabling the code and issuing a new one
that nobody had.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.bot.handlers.admin.gifts import _render_detail
from app.database.models.gift import GiftCode, GiftItem, GiftStatus
from app.database.repositories.gift_repo import GiftRepo
from app.database.repositories.user_repo import UserRepo
from app.services.gift_service import (
    add_items,
    available_item_count,
    create_gift_code,
    delete_gift_code,
    update_gift_code,
)
from app.utils.text import PAD


async def _item_code(sessionmaker, items: list[str]) -> int:
    async with sessionmaker() as session:
        await create_gift_code(
            session, item_payloads=items, currency="USD", max_uses=0,
            per_user_limit=1, expires_at=None, admin_id=999,
        )
        await session.commit()
        return max(g.id for g in await GiftRepo(session).list_all())


async def _credit_code(sessionmaker) -> int:
    async with sessionmaker() as session:
        await create_gift_code(
            session, value_minor=100, currency="USD", max_uses=2,
            per_user_limit=1, expires_at=None, admin_id=999,
        )
        await session.commit()
        return max(g.id for g in await GiftRepo(session).list_all())


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ---- The detail screen ----


@pytest.mark.asyncio
async def test_the_detail_screen_offers_every_safe_edit(sqlite_sessionmaker):
    gift_id = await _credit_code(sqlite_sessionmaker)

    async with sqlite_sessionmaker() as session:
        text, markup = await _render_detail(session, gift_id)

    targets = _callbacks(markup)
    assert f"gedit:ds:{gift_id}" in targets, "description"
    assert f"gedit:pu:{gift_id}" in targets, "per-user limit"
    assert f"gedit:ex:{gift_id}" in targets, "expiry"
    assert f"agift:toggle:{gift_id}" in targets
    assert f"agift:delete:{gift_id}" in targets
    assert f"agift:delete_ok:{gift_id}" not in targets, "destructive must not be one tap"


@pytest.mark.asyncio
async def test_the_bubble_is_padded_to_the_button_width(sqlite_sessionmaker):
    """Without this the bubble is narrower than its own button column and the labels float."""
    gift_id = await _credit_code(sqlite_sessionmaker)

    async with sqlite_sessionmaker() as session:
        text, _ = await _render_detail(session, gift_id)

    assert text.endswith(PAD)


@pytest.mark.asyncio
async def test_only_an_item_code_offers_add_items(sqlite_sessionmaker):
    credit_id = await _credit_code(sqlite_sessionmaker)
    async with sqlite_sessionmaker() as session:
        text, markup = await _render_detail(session, credit_id)
        assert not any("additems" in (c or "") for c in _callbacks(markup))

    item_id = await _item_code(sqlite_sessionmaker, ["A"])
    async with sqlite_sessionmaker() as session:
        text, markup = await _render_detail(session, item_id)
        assert f"agift:additems:{item_id}" in _callbacks(markup)


# ---- Editing ----


@pytest.mark.asyncio
async def test_the_description_can_be_fixed(sqlite_sessionmaker):
    gift_id = await _credit_code(sqlite_sessionmaker)

    async with sqlite_sessionmaker() as session:
        await update_gift_code(session, gift_id, description="Nothing to claim, sorry!")
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert (await session.get(GiftCode, gift_id)).description == "Nothing to claim, sorry!"


@pytest.mark.asyncio
async def test_extending_the_expiry_revives_an_expired_code(sqlite_sessionmaker):
    """Otherwise the edit does nothing visible — the code stays EXPIRED and unclaimable."""
    gift_id = await _credit_code(sqlite_sessionmaker)

    async with sqlite_sessionmaker() as session:
        gift = await session.get(GiftCode, gift_id)
        gift.expires_at = datetime.now(UTC) - timedelta(days=1)
        gift.status = GiftStatus.EXPIRED
        await session.commit()

    async with sqlite_sessionmaker() as session:
        await update_gift_code(session, gift_id, expires_at=datetime.now(UTC) + timedelta(days=7))
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert (await session.get(GiftCode, gift_id)).status is GiftStatus.ACTIVE


@pytest.mark.asyncio
async def test_editing_one_field_leaves_the_others_alone(sqlite_sessionmaker):
    """`None` is a real value for description and expiry, so the "leave alone" sentinel cannot be
    None — this is what that sentinel is for."""
    gift_id = await _credit_code(sqlite_sessionmaker)

    async with sqlite_sessionmaker() as session:
        await update_gift_code(
            session, gift_id, description="keep me", expires_at=datetime.now(UTC) + timedelta(days=3)
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        await update_gift_code(session, gift_id, per_user_limit=5)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        gift = await session.get(GiftCode, gift_id)
        assert gift.per_user_limit == 5
        assert gift.description == "keep me"
        assert gift.expires_at is not None


# ---- Topping up ----


@pytest.mark.asyncio
async def test_adding_items_extends_the_code_people_already_hold(sqlite_sessionmaker):
    gift_id = await _item_code(sqlite_sessionmaker, ["A", "B"])

    async with sqlite_sessionmaker() as session:
        assert await add_items(session, gift_id, ["C", "D", "E"]) == 3
        await session.commit()

    async with sqlite_sessionmaker() as session:
        gift = await session.get(GiftCode, gift_id)
        assert gift.max_uses == 5, "redemptions grow with the items"
        assert await available_item_count(session, gift_id) == 5


@pytest.mark.asyncio
async def test_topping_up_revives_an_exhausted_code(sqlite_sessionmaker):
    gift_id = await _item_code(sqlite_sessionmaker, ["ONLY-ONE"])
    async with sqlite_sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=9301, username="u", first_name="T", last_name=None,
            chat_id=9301, default_locale="en",
        )
        await session.commit()

    async with sqlite_sessionmaker() as session:
        gift = await session.get(GiftCode, gift_id)
        gift.status = GiftStatus.EXHAUSTED
        await session.commit()

    async with sqlite_sessionmaker() as session:
        await add_items(session, gift_id, ["MORE-1"])
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert (await session.get(GiftCode, gift_id)).status is GiftStatus.ACTIVE


@pytest.mark.asyncio
async def test_a_credit_code_cannot_be_topped_up(sqlite_sessionmaker):
    gift_id = await _credit_code(sqlite_sessionmaker)

    async with sqlite_sessionmaker() as session:
        with pytest.raises(ValueError):
            await add_items(session, gift_id, ["A"])


# ---- Disable and delete ----


@pytest.mark.asyncio
async def test_deleting_takes_the_code_and_its_items(sqlite_sessionmaker):
    gift_id = await _item_code(sqlite_sessionmaker, ["A", "B"])

    async with sqlite_sessionmaker() as session:
        await delete_gift_code(session, gift_id)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await session.get(GiftCode, gift_id) is None
        assert (await session.execute(select(GiftItem))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_claimed_code_can_still_be_deleted(sqlite_sessionmaker):
    """The redemption rows point at it, so deleting has to clear them or the FK blocks it."""
    gift_id = await _item_code(sqlite_sessionmaker, ["A"])
    async with sqlite_sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=9302, username="u", first_name="T", last_name=None,
            chat_id=9302, default_locale="en",
        )
        await session.commit()
        user_id = user.id

    async with sqlite_sessionmaker() as session:
        # Claimed by id: the plaintext code is not recoverable after creation.
        from app.services.gift_service import redeem_gift_by_id

        await redeem_gift_by_id(session, user_id=user_id, gift_id=gift_id)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        await delete_gift_code(session, gift_id)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await session.get(GiftCode, gift_id) is None
