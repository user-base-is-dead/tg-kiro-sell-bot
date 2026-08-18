"""Two failures on the gift-item branch, and the item manager that followed them.

1. Pasting the items crashed with "Something went wrong on our end". Every prompt after the kind
   question called `_step()` to render "(step N of M)" — a helper that did not exist. The item
   branch died on its very first answer, and the credit branch on its second.
2. The review and success screens still spoke the language of the deleted PRODUCT kind
   (`is_product`, `data["product_name"]`), so even past the first crash the wizard could not finish.
3. Once created, the item pool was write-only: an admin could top it up but never see, fix, or
   remove a line — a typo'd or revoked key had to be lived with, or the whole code thrown away.
"""

from __future__ import annotations

import pytest

from app.bot.handlers.admin.gifts import (
    _SKIP_STATES,
    _SKIPS,
    _skip_keyboard,
    _step,
    _wizard_grant,
    _wizard_max_uses,
)
from app.core.security import get_cipher
from app.database.models.gift import GiftItemStatus, GiftStatus
from app.database.repositories.gift_repo import GiftRepo
from app.services.gift_service import (
    available_item_count,
    count_items,
    create_gift_code,
    delete_item,
    list_items,
    update_item_payload,
)


async def _code(sessionmaker, items: list[str]) -> int:
    async with sessionmaker() as session:
        await create_gift_code(
            session, item_payloads=items, currency="USD", max_uses=0,
            per_user_limit=1, expires_at=None, admin_id=999,
        )
        await session.commit()
        return (await GiftRepo(session).list_all())[0].id


# ---- The wizard survives its own prompts ----


def test_every_prompt_can_render_its_step_counter() -> None:
    """The crash: each of these was a NameError in production."""
    item = {"kind": "item", "items": ["A", "B"]}
    assert _step(item, "items") == "<i>(step 2 of 5)</i>"
    assert _step(item, "per_user_limit") == "<i>(step 3 of 5)</i>"
    assert _step(item, "expires") == "<i>(step 4 of 5)</i>"
    assert _step(item, "description") == "<i>(step 5 of 5)</i>"

    credit = {"kind": "credit"}
    assert _step(credit, "value") == "<i>(step 2 of 6)</i>"
    assert _step(credit, "max_uses") == "<i>(step 3 of 6)</i>"
    assert _step(credit, "description") == "<i>(step 6 of 6)</i>"


def test_the_review_screen_needs_no_product_fields() -> None:
    """`data["product_name"]` and `data["value_minor"]` are not collected on the item branch."""
    data = {"kind": "item", "items": ["A", "B", "C"], "per_user_limit": 1}
    assert _wizard_grant(data) == "🎁 Gift item — 3 item(s)"
    # The item count *is* the redemption count; the branch never asks for `max_uses`.
    assert _wizard_max_uses(data) == 3


# ---- Seeing and fixing what a code hands out ----


@pytest.mark.asyncio
async def test_the_pool_can_be_counted_and_read_back(sqlite_sessionmaker):
    gift_id = await _code(sqlite_sessionmaker, ["KEY-A", "KEY-B"])
    async with sqlite_sessionmaker() as session:
        assert await count_items(session, gift_id) == (2, 2)
        cipher = get_cipher()
        assert [cipher.decrypt(i.payload) for i in await list_items(session, gift_id)] == ["KEY-A", "KEY-B"]


@pytest.mark.asyncio
async def test_an_unclaimed_item_can_be_rewritten_in_place(sqlite_sessionmaker):
    gift_id = await _code(sqlite_sessionmaker, ["TYPO"])
    async with sqlite_sessionmaker() as session:
        item = (await list_items(session, gift_id))[0]
        assert await update_item_payload(session, item.id, "KEY-FIXED") == gift_id
        await session.commit()

    async with sqlite_sessionmaker() as session:
        stored = (await list_items(session, gift_id))[0].payload
        assert get_cipher().decrypt(stored) == "KEY-FIXED"
        assert stored != "KEY-FIXED", "still encrypted at rest"


@pytest.mark.asyncio
async def test_removing_an_item_lowers_the_redemption_count(sqlite_sessionmaker):
    """The mirror of `add_items`: on this kind of code the two numbers are the same thing, and
    leaving `max_uses` high would let a claimer burn their redemption on nothing."""
    gift_id = await _code(sqlite_sessionmaker, ["A", "B", "C"])
    async with sqlite_sessionmaker() as session:
        item = (await list_items(session, gift_id))[0]
        await delete_item(session, item.id)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        gift = await GiftRepo(session).get_by_id(gift_id)
        assert gift.max_uses == 2
        assert await available_item_count(session, gift_id) == 2


@pytest.mark.asyncio
async def test_removing_the_last_item_exhausts_the_code(sqlite_sessionmaker):
    gift_id = await _code(sqlite_sessionmaker, ["ONLY"])
    async with sqlite_sessionmaker() as session:
        await delete_item(session, (await list_items(session, gift_id))[0].id)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert (await GiftRepo(session).get_by_id(gift_id)).status is GiftStatus.EXHAUSTED


@pytest.mark.asyncio
async def test_a_delivered_item_is_frozen(sqlite_sessionmaker):
    """Its claimer was shown that exact value. Rewriting or deleting the row would leave the bot
    disagreeing with what that person actually holds."""
    gift_id = await _code(sqlite_sessionmaker, ["GIVEN"])
    async with sqlite_sessionmaker() as session:
        item = (await list_items(session, gift_id))[0]
        item.status = GiftItemStatus.DELIVERED
        await session.commit()
        item_id = item.id

    async with sqlite_sessionmaker() as session:
        with pytest.raises(ValueError):
            await update_item_payload(session, item_id, "REWRITTEN")
        with pytest.raises(ValueError):
            await delete_item(session, item_id)


# ---- The last three questions answer themselves ----


def test_every_default_button_belongs_to_the_step_it_answers() -> None:
    """The prompts stay in the chat, so an earlier screen's button is still tappable. The handler
    refuses one that does not match the current state — this pins the pairing it checks against."""
    assert set(_SKIPS) == set(_SKIP_STATES)
    assert _SKIP_STATES["pu"].state.endswith(":per_user_limit")
    assert _SKIP_STATES["ex"].state.endswith(":expires_days")
    assert _SKIP_STATES["ds"].state.endswith(":description")


def test_the_defaults_are_the_answer_almost_every_code_wants() -> None:
    fields = {key: (field, default) for key, (field, default, _, _) in _SKIPS.items()}
    assert fields["pu"] == ("per_user_limit", 1)
    assert fields["ex"] == ("expires_days", 0)  # 0 days is the wizard's "never"
    assert fields["ds"] == ("description", None)


def test_a_default_button_offers_an_abort_beside_it() -> None:
    """A screen whose only button commits to something is a trap — /cancel is documented but a
    button is what people reach for."""
    rows = _skip_keyboard("pu", "1️⃣ Once per user").inline_keyboard
    assert rows[0][0].callback_data == "gskip:pu"
    assert len(rows) == 2, "the abort row is not optional"
