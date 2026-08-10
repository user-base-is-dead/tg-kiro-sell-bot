"""The warranty clock is a stored instant, not a running counter.

Every scenario here is written as "what does the system conclude at time T", with T injected
rather than slept for. That is the whole point of the design: the answer is a pure function of
`warranties.expires_at` and the current time, so a process that was dead for six hours reaches the
same conclusion as one that never stopped. Nothing in this file advances a timer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.bot.handlers.warranty.screen import _render_warranty
from app.database.models.order import Order, OrderItem, OrderStatus, Warranty, WarrantyStatus
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.warranty_repo import WarrantyRepo
from app.services.warranty_service import (
    CLAIM_GRACE,
    display_remaining,
    open_claim,
    reject_claim,
    remaining_seconds,
    resolve_claim,
)

# The spec's worked example: bought Monday 10:00, 24h warranty, so it dies Tuesday 10:00.
PURCHASE = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
EXPIRY = PURCHASE + timedelta(hours=24)


async def _user(sessionmaker, telegram_id: int) -> int:
    async with sessionmaker() as session:
        user, _ = await UserRepo(session).upsert_from_telegram(
            telegram_id=telegram_id,
            username=f"u{telegram_id}",
            first_name="T",
            last_name=None,
            chat_id=telegram_id,
            default_locale="en",
        )
        await session.commit()
        return user.id


async def _warranty(
    sessionmaker,
    user_id: int,
    *,
    starts_at: datetime = PURCHASE,
    expires_at: datetime = EXPIRY,
    warranty_days: int = 1,
    name: str = "Kiro Pro",
    seq: int = 0,
) -> int:
    async with sessionmaker() as session:
        order = Order(
            order_number=f"ORD-{user_id}-{seq}",
            user_id=user_id,
            status=OrderStatus.COMPLETED,
            subtotal_minor=0,
            discount_minor=0,
            total_minor=0,
            currency="USD",
            idempotency_key=f"t:{user_id}:{seq}",
            placed_at=starts_at,
        )
        session.add(order)
        await session.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=None,
            product_name=name,
            unit_price_minor=0,
            qty=1,
            warranty_days=warranty_days,
        )
        session.add(item)
        await session.flush()
        warranty = Warranty(
            order_item_id=item.id,
            user_id=user_id,
            starts_at=starts_at,
            expires_at=expires_at,
            status=WarrantyStatus.ACTIVE,
        )
        session.add(warranty)
        await session.commit()
        return warranty.id


@pytest.mark.asyncio
async def test_downtime_does_not_hand_back_warranty_time(sqlite_sessionmaker):
    """Offline from Monday 22:00 to Tuesday 10:00 — the warranty is spent, not paused."""
    user_id = await _user(sqlite_sessionmaker, 7101)
    warranty_id = await _warranty(sqlite_sessionmaker, user_id)

    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        assert remaining_seconds(w, PURCHASE + timedelta(hours=12)) == 12 * 3600
        # Comes back up exactly at expiry, having missed every tick in between.
        assert remaining_seconds(w, EXPIRY) == 0
        assert remaining_seconds(w, EXPIRY + timedelta(hours=5)) == 0


@pytest.mark.asyncio
async def test_filing_a_claim_freezes_the_payout_but_not_the_warranty(sqlite_sessionmaker):
    """Claim at 22:00 with 12h left: the frozen figure is 12h and `expires_at` is untouched."""
    user_id = await _user(sqlite_sessionmaker, 7102)
    warranty_id = await _warranty(sqlite_sessionmaker, user_id)
    claim_at = PURCHASE + timedelta(hours=12)

    async with sqlite_sessionmaker() as session:
        repo = WarrantyRepo(session)
        w = await repo.get_by_id(warranty_id)
        open_claim(w, ticket_id=1, at=claim_at)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        assert w.status is WarrantyStatus.CLAIMED
        assert w.claim_remaining_seconds == 12 * 3600
        assert w.expires_at.replace(tzinfo=UTC) == EXPIRY, "the claim must not move the expiry"
        assert w.claim_deadline_at.replace(tzinfo=UTC) == claim_at + CLAIM_GRACE
        # Customer sees the frozen number; the real clock kept running underneath it.
        assert "on hold" in display_remaining(w, EXPIRY)
        assert remaining_seconds(w, EXPIRY) == 0


@pytest.mark.asyncio
async def test_done_rebases_the_held_time_onto_the_replacement(sqlite_sessionmaker):
    """Spec §13 step 3B: 12h held, /done at 18:00, replacement dies 06:00 next day."""
    user_id = await _user(sqlite_sessionmaker, 7103)
    warranty_id = await _warranty(sqlite_sessionmaker, user_id)
    claim_at = PURCHASE + timedelta(hours=12)
    done_at = PURCHASE + timedelta(hours=20)  # 06:00 Tuesday, still inside the original window

    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        open_claim(w, ticket_id=1, at=claim_at)
        outcome = resolve_claim(w, at=done_at)
        await session.commit()

    assert outcome.granted_seconds == 12 * 3600

    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        assert w.status is WarrantyStatus.ACTIVE
        assert w.expires_at.replace(tzinfo=UTC) == done_at + timedelta(hours=12)
        assert w.claim_resolved_at is not None
        assert w.claim_deadline_at is None, "the staff clock is done with"


@pytest.mark.asyncio
async def test_done_after_the_original_expiry_grants_nothing(sqlite_sessionmaker):
    """The frozen 12h is a cap on the payout, not a promise — the warranty still had to survive."""
    user_id = await _user(sqlite_sessionmaker, 7104)
    warranty_id = await _warranty(sqlite_sessionmaker, user_id)

    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        open_claim(w, ticket_id=1, at=PURCHASE + timedelta(hours=12))
        outcome = resolve_claim(w, at=EXPIRY + timedelta(hours=1))
        await session.commit()

    assert outcome.granted_seconds == 0

    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        assert w.status is WarrantyStatus.EXPIRED
        assert w.expires_at.replace(tzinfo=UTC) == EXPIRY


@pytest.mark.asyncio
async def test_reject_after_expiry_restores_nothing(sqlite_sessionmaker):
    """Spec §13 step 3A: claimed at 22:00 with 12h left, rejected Tuesday 11:00 → void, 0 left."""
    user_id = await _user(sqlite_sessionmaker, 7105)
    warranty_id = await _warranty(sqlite_sessionmaker, user_id)

    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        open_claim(w, ticket_id=1, at=PURCHASE + timedelta(hours=12))
        outcome = reject_claim(w, reason="not covered", at=EXPIRY + timedelta(hours=1))
        await session.commit()

    assert outcome.granted_seconds == 0

    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        assert w.status is WarrantyStatus.EXPIRED
        assert w.claim_notes == "not covered"
        assert w.claim_rejected_at is not None


@pytest.mark.asyncio
async def test_reject_while_time_remains_resumes_the_original_timeline(sqlite_sessionmaker):
    """Not restarted, not extended: the same `expires_at` it has had since purchase."""
    user_id = await _user(sqlite_sessionmaker, 7106)
    warranty_id = await _warranty(sqlite_sessionmaker, user_id)
    reject_at = PURCHASE + timedelta(hours=20)

    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        open_claim(w, ticket_id=1, at=PURCHASE + timedelta(hours=12))
        outcome = reject_claim(w, reason="no fault found", at=reject_at)
        await session.commit()

    assert outcome.granted_seconds == 4 * 3600, "4h of the original 24 are genuinely left"

    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        assert w.status is WarrantyStatus.ACTIVE
        assert w.expires_at.replace(tzinfo=UTC) == EXPIRY
        assert w.claim_remaining_seconds is None


@pytest.mark.asyncio
async def test_the_claim_deadline_is_a_separate_clock_from_the_warranty(sqlite_sessionmaker):
    """A claim filed on a 30-day warranty still has only the staff grace window to be answered."""
    user_id = await _user(sqlite_sessionmaker, 7107)
    long_expiry = PURCHASE + timedelta(days=30)
    warranty_id = await _warranty(sqlite_sessionmaker, user_id, expires_at=long_expiry, warranty_days=30)
    claim_at = PURCHASE + timedelta(days=1)

    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        open_claim(w, ticket_id=1, at=claim_at)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        repo = WarrantyRepo(session)
        assert await repo.list_claims_past_deadline(claim_at + CLAIM_GRACE - timedelta(minutes=1)) == []
        stale = await repo.list_claims_past_deadline(claim_at + CLAIM_GRACE)
        assert [w.id for w in stale] == [warranty_id]

    # Auto-closing on that deadline leaves the still-healthy 30-day warranty alone.
    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        reject_claim(w, reason="timed out", at=claim_at + CLAIM_GRACE)
        await session.commit()

    async with sqlite_sessionmaker() as session:
        w = await WarrantyRepo(session).get_by_id(warranty_id)
        assert w.status is WarrantyStatus.ACTIVE
        assert w.expires_at.replace(tzinfo=UTC) == long_expiry


@pytest.mark.asyncio
async def test_warranties_are_listed_oldest_first_and_paged_by_twelve(sqlite_sessionmaker):
    user_id = await _user(sqlite_sessionmaker, 7108)
    for seq in range(15):
        await _warranty(sqlite_sessionmaker, user_id, name=f"Item {seq}", seq=seq)

    async with sqlite_sessionmaker() as session:
        repo = WarrantyRepo(session)
        assert await repo.count_for_user(user_id) == 15

        first = await repo.list_for_user(user_id, limit=12, offset=0)
        assert len(first) == 12
        assert [w.order_item.product_name for w in first[:2]] == ["Item 0", "Item 1"]

        second = await repo.list_for_user(user_id, limit=12, offset=12)
        assert [w.order_item.product_name for w in second] == ["Item 12", "Item 13", "Item 14"]


@pytest.mark.asyncio
async def test_the_screen_offers_a_claim_button_for_every_item_on_the_page(sqlite_sessionmaker):
    """Including the expired one — hiding the button leaves the customer no way to ask why."""
    user_id = await _user(sqlite_sessionmaker, 7109)
    for seq in range(14):
        await _warranty(sqlite_sessionmaker, user_id, name=f"Item {seq}", seq=seq)
    # Item 3 is long dead; it must still render, and still be clickable.
    dead_id = await _warranty(
        sqlite_sessionmaker, user_id, name="Dead One", seq=99, expires_at=PURCHASE - timedelta(days=1)
    )

    async with sqlite_sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        repo = WarrantyRepo(session)

        text, markup = await _render_warranty(repo, user, page_num=1)
        claim_rows = [r for r in markup.inline_keyboard if r[0].callback_data.startswith("wclaim:")]
        assert len(claim_rows) == 12, "one claim button per listed warranty"
        assert "1/2" in text
        nav = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "warranty_page:2" in nav
        assert not any(cb == "warranty_page:0" for cb in nav), "no Prev on page 1"

        text, markup = await _render_warranty(repo, user, page_num=2)
        claim_rows = [r for r in markup.inline_keyboard if r[0].callback_data.startswith("wclaim:")]
        assert len(claim_rows) == 3
        assert f"wclaim:{dead_id}" in [r[0].callback_data for r in claim_rows]
        assert "EXPIRED" in text
        nav = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "warranty_page:1" in nav


@pytest.mark.asyncio
async def test_an_empty_warranty_screen_still_renders(sqlite_sessionmaker):
    user_id = await _user(sqlite_sessionmaker, 7110)
    async with sqlite_sessionmaker() as session:
        user = await UserRepo(session).get_by_id(user_id)
        text, _ = await _render_warranty(WarrantyRepo(session), user)
        assert "don't have any warranties yet" in text
