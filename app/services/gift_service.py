from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import delete, func, select, update

from app.core.security import get_cipher, hash_gift_code, new_gift_code
from app.database.models.gift import (
    GiftCode,
    GiftItem,
    GiftItemStatus,
    GiftKind,
    GiftRedemption,
    GiftStatus,
)
from app.database.models.order import Order
from app.database.models.wallet import TxnType
from app.database.repositories.gift_repo import GiftRepo
from app.services import wallet_service
from app.utils.errors import UserError
from app.utils.time import as_utc


async def create_gift_code(
    session: AsyncSession,
    *,
    currency: str,
    max_uses: int,
    per_user_limit: int,
    expires_at: datetime | None,
    admin_id: int,
    value_minor: int | None = None,
    item_payloads: list[str] | None = None,
    description: str | None = None,
) -> str:
    """Returns the plaintext code — shown to the admin exactly once, never recoverable
    afterwards (only the hash + last 4 chars are persisted).

    Exactly one of `value_minor` / `item_payloads` must be given: that pairing is what makes `kind`
    meaningful, and this is the only place it is enforced, so a malformed code can never reach the
    database and fail at claim time in front of a user.

    For an ITEM code `max_uses` is ignored and set to the number of items. One claim hands over one
    item, so any other number is a promise the code cannot keep in one direction or the other — too
    high and a claimer burns their redemption on nothing, too low and paid-for items are stranded.
    """
    if (value_minor is None) == (item_payloads is None):
        raise ValueError("A gift code grants either wallet credit or its own items, not both or neither")

    if value_minor is not None:
        kind = GiftKind.CREDIT
        if value_minor <= 0:
            raise ValueError("Credit gift codes must carry a positive value")
    else:
        kind = GiftKind.ITEM
        item_payloads = [line.strip() for line in item_payloads if line.strip()]
        if not item_payloads:
            raise ValueError("An item gift code must carry at least one item")
        max_uses = len(item_payloads)

    plaintext = new_gift_code()
    gift = GiftCode(
        code_hash=hash_gift_code(plaintext),
        code_last4=plaintext[-4:],
        kind=kind,
        value_minor=value_minor,
        currency=currency,
        max_uses=max_uses,
        per_user_limit=per_user_limit,
        expires_at=expires_at,
        status=GiftStatus.ACTIVE,
        created_by_admin_id=admin_id,
        description=description,
    )
    session.add(gift)
    await session.flush()

    if kind is GiftKind.ITEM:
        cipher = get_cipher()
        for payload in item_payloads:
            session.add(GiftItem(gift_code_id=gift.id, payload=cipher.encrypt(payload)))
        await session.flush()

    return plaintext


async def available_item_count(session: AsyncSession, gift_id: int) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(GiftItem)
        .where(GiftItem.gift_code_id == gift_id, GiftItem.status == GiftItemStatus.AVAILABLE)
    )
    return int(result.scalar_one())


async def get_active_gift(session: AsyncSession) -> GiftCode | None:
    """Get the first active gift code, if any."""
    repo = GiftRepo(session)
    now = datetime.now(UTC)
    gifts = await repo.list_all()
    for gift in gifts:
        if gift.status != GiftStatus.ACTIVE:
            continue
        if gift.expires_at is not None and as_utc(gift.expires_at) < now:
            continue
        if gift.used_count >= gift.max_uses:
            continue
        return gift
    return None


@dataclass(frozen=True)
class ClaimedGift:
    """What a claim produced. `delivered_payload` is plaintext shown to the user exactly once and
    never re-fetchable. `order` is always None now — a giveaway is not a purchase and creates no
    order; `gift_items.claimed_by_user_id` is the record of who received what."""

    gift: GiftCode
    delivered_payload: str | None = None
    order: Order | None = None


async def _claim(session: AsyncSession, gift: GiftCode | None, user_id: int) -> ClaimedGift:
    """Eligibility checks and the grant itself, shared by both entry points so the rules can't
    drift between claiming by button and claiming by typed code."""
    if gift is None:
        raise UserError("gift.invalid_code")

    now = datetime.now(UTC)
    if gift.status != GiftStatus.ACTIVE:
        raise UserError("gift.not_active")
    if gift.expires_at is not None and as_utc(gift.expires_at) < now:
        gift.status = GiftStatus.EXPIRED
        await session.flush()
        raise UserError("gift.expired")
    if gift.used_count >= gift.max_uses:
        gift.status = GiftStatus.EXHAUSTED
        await session.flush()
        raise UserError("gift.exhausted")

    repo = GiftRepo(session)
    already_used = await repo.count_redemptions_for_user(gift.id, user_id)
    if already_used >= gift.per_user_limit:
        raise UserError("gift.limit_reached")

    if gift.kind is GiftKind.ITEM:
        result = await _grant_item(session, gift, user_id, now)
    else:
        result = await _grant_credit(session, gift, user_id, already_used, now)

    gift.used_count += 1
    if gift.used_count >= gift.max_uses:
        gift.status = GiftStatus.EXHAUSTED

    await session.flush()
    return result


async def _grant_credit(
    session: AsyncSession, gift: GiftCode, user_id: int, already_used: int, now: datetime
) -> ClaimedGift:
    txn = await wallet_service.credit(
        session,
        user_id=user_id,
        amount_minor=gift.value_minor,
        currency=gift.currency,
        type_=TxnType.GIFT,
        idempotency_key=f"gift:{gift.id}:{user_id}:{already_used}",
        ref_type="gift_code",
        ref_id=str(gift.id),
    )
    session.add(
        GiftRedemption(gift_code_id=gift.id, user_id=user_id, wallet_transaction_id=txn.id, redeemed_at=now)
    )
    return ClaimedGift(gift=gift)


async def _grant_item(session: AsyncSession, gift: GiftCode, user_id: int, now: datetime) -> ClaimedGift:
    """Hand over one of the code's own items. Nothing in the catalog moves.

    The claim is a conditional UPDATE on one specific row, so two people pressing Claim at the same
    instant cannot be given the same line — the loser finds no row left to take and is told the
    giveaway ran out rather than receiving a duplicate.
    """
    candidate = (
        await session.execute(
            select(GiftItem.id)
            .where(GiftItem.gift_code_id == gift.id, GiftItem.status == GiftItemStatus.AVAILABLE)
            .order_by(GiftItem.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if candidate is None:
        # Nothing left. The code is marked exhausted so the next person is turned away up front.
        gift.status = GiftStatus.EXHAUSTED
        await session.flush()
        raise UserError("gift.exhausted")

    claimed = await session.execute(
        update(GiftItem)
        .where(GiftItem.id == candidate, GiftItem.status == GiftItemStatus.AVAILABLE)
        .values(status=GiftItemStatus.DELIVERED, claimed_by_user_id=user_id, claimed_at=now)
    )
    if not claimed.rowcount:
        raise UserError("gift.exhausted")

    item = await session.get(GiftItem, candidate)
    session.add(
        GiftRedemption(
            gift_code_id=gift.id,
            user_id=user_id,
            wallet_transaction_id=None,
            order_id=None,
            redeemed_at=now,
        )
    )
    return ClaimedGift(gift=gift, delivered_payload=get_cipher().decrypt(item.payload))


async def redeem_gift_by_id(session: AsyncSession, *, user_id: int, gift_id: int) -> ClaimedGift:
    """Claim a gift directly by ID (used for the claim button UI)."""
    return await _claim(session, await GiftRepo(session).get_by_id(gift_id), user_id)


async def redeem_gift_code(session: AsyncSession, *, user_id: int, code_plaintext: str) -> ClaimedGift:
    gift = await GiftRepo(session).get_by_hash(hash_gift_code(code_plaintext.strip().upper()))
    return await _claim(session, gift, user_id)


async def add_items(session: AsyncSession, gift_id: int, payloads: list[str]) -> int:
    """Top an ITEM code up with more items. Returns how many were added.

    `max_uses` moves with the item count, because on this kind of code they are the same number —
    one claim hands over one item. Topping up therefore extends the code people already hold rather
    than forcing a second giveaway with a new code to distribute.
    """
    gift = await session.get(GiftCode, gift_id)
    if gift is None or gift.kind is not GiftKind.ITEM:
        raise ValueError("Only an item gift code can be topped up")

    clean = [line.strip() for line in payloads if line.strip()]
    if not clean:
        raise ValueError("Send at least one item")

    cipher = get_cipher()
    for payload in clean:
        session.add(GiftItem(gift_code_id=gift.id, payload=cipher.encrypt(payload)))
    gift.max_uses += len(clean)
    # A code that ran dry is claimable again the moment it has stock, without an extra admin step.
    if gift.status is GiftStatus.EXHAUSTED:
        gift.status = GiftStatus.ACTIVE
    await session.flush()
    return len(clean)


async def update_gift_code(
    session: AsyncSession,
    gift_id: int,
    *,
    description: str | None = ...,
    per_user_limit: int | None = None,
    expires_at: datetime | None = ...,
) -> None:
    """Edit the fields that are safe to change after a code is in circulation.

    Deliberately not editable: `kind`, `value_minor`, and the item pool. Changing what a code grants
    out from under someone who already has it is a different code, not an edit — issue a new one.
    `...` is the "leave alone" sentinel because None is a real value for both nullable fields.
    """
    gift = await session.get(GiftCode, gift_id)
    if gift is None:
        raise ValueError("No such gift code")

    if description is not ...:
        gift.description = description
    if per_user_limit is not None:
        gift.per_user_limit = per_user_limit
    if expires_at is not ...:
        gift.expires_at = expires_at
        # An expiry moved into the future should un-expire the code, or the edit does nothing.
        if gift.status is GiftStatus.EXPIRED and (expires_at is None or expires_at > datetime.now(UTC)):
            gift.status = GiftStatus.ACTIVE
    await session.flush()


async def delete_gift_code(session: AsyncSession, gift_id: int) -> None:
    """Remove a code and everything that belongs to it.

    `gift_items.gift_code_id` is NOT NULL, so claimed rows cannot be detached and kept — they go
    too. What a claimer already received is not lost by this: the payload was shown to them at claim
    time and is theirs. What is lost is the bot's own record of it, which is why the admin screen
    offers **Disable** first and spells out the difference. Use delete for a code that was a mistake;
    disable for one that has served its purpose.
    """
    await session.execute(delete(GiftRedemption).where(GiftRedemption.gift_code_id == gift_id))
    await session.execute(delete(GiftItem).where(GiftItem.gift_code_id == gift_id))
    gift = await session.get(GiftCode, gift_id)
    if gift is not None:
        await session.delete(gift)
    await session.flush()
