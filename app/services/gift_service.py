from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_gift_code, new_gift_code
from app.database.models.gift import GiftCode, GiftRedemption, GiftStatus
from app.database.models.wallet import TxnType
from app.database.repositories.gift_repo import GiftRepo
from app.services import wallet_service
from app.utils.errors import UserError


async def create_gift_code(
    session: AsyncSession,
    *,
    value_minor: int,
    currency: str,
    max_uses: int,
    per_user_limit: int,
    expires_at: datetime | None,
    admin_id: int,
    description: str | None = None,
) -> str:
    """Returns the plaintext code — shown to the admin exactly once, never recoverable
    afterwards (only the hash + last 4 chars are persisted)."""
    plaintext = new_gift_code()
    gift = GiftCode(
        code_hash=hash_gift_code(plaintext),
        code_last4=plaintext[-4:],
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
    return plaintext


async def get_active_gift(session: AsyncSession) -> GiftCode | None:
    """Get the first active gift code, if any."""
    repo = GiftRepo(session)
    now = datetime.now(UTC)
    gifts = await repo.list_all()
    for gift in gifts:
        if gift.status != GiftStatus.ACTIVE:
            continue
        if gift.expires_at is not None and gift.expires_at < now:
            continue
        if gift.used_count >= gift.max_uses:
            continue
        return gift
    return None


async def redeem_gift_by_id(session: AsyncSession, *, user_id: int, gift_id: int) -> GiftCode:
    """Claim a gift directly by ID (used for the claim button UI)."""
    repo = GiftRepo(session)
    gift = await repo.get_by_id(gift_id)
    if gift is None:
        raise UserError("gift.invalid_code")

    now = datetime.now(UTC)
    if gift.status != GiftStatus.ACTIVE:
        raise UserError("gift.not_active")
    if gift.expires_at is not None and gift.expires_at < now:
        gift.status = GiftStatus.EXPIRED
        await session.flush()
        raise UserError("gift.expired")
    if gift.used_count >= gift.max_uses:
        gift.status = GiftStatus.EXHAUSTED
        await session.flush()
        raise UserError("gift.exhausted")

    already_used = await repo.count_redemptions_for_user(gift.id, user_id)
    if already_used >= gift.per_user_limit:
        raise UserError("gift.limit_reached")

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
    session.add(GiftRedemption(gift_code_id=gift.id, user_id=user_id, wallet_transaction_id=txn.id, redeemed_at=now))

    gift.used_count += 1
    if gift.used_count >= gift.max_uses:
        gift.status = GiftStatus.EXHAUSTED

    await session.flush()
    return gift


async def redeem_gift_code(session: AsyncSession, *, user_id: int, code_plaintext: str) -> GiftCode:
    repo = GiftRepo(session)
    gift = await repo.get_by_hash(hash_gift_code(code_plaintext.strip().upper()))
    if gift is None:
        raise UserError("gift.invalid_code")

    now = datetime.now(UTC)
    if gift.status != GiftStatus.ACTIVE:
        raise UserError("gift.not_active")
    if gift.expires_at is not None and gift.expires_at < now:
        gift.status = GiftStatus.EXPIRED
        await session.flush()
        raise UserError("gift.expired")
    if gift.used_count >= gift.max_uses:
        gift.status = GiftStatus.EXHAUSTED
        await session.flush()
        raise UserError("gift.exhausted")

    already_used = await repo.count_redemptions_for_user(gift.id, user_id)
    if already_used >= gift.per_user_limit:
        raise UserError("gift.limit_reached")

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
    session.add(GiftRedemption(gift_code_id=gift.id, user_id=user_id, wallet_transaction_id=txn.id, redeemed_at=now))

    gift.used_count += 1
    if gift.used_count >= gift.max_uses:
        gift.status = GiftStatus.EXHAUSTED

    await session.flush()
    return gift
