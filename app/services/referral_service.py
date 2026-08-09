from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.order import OrderStatus
from app.database.models.wallet import TxnType
from app.database.models.referral import Referral
from app.core.config import get_settings
from app.database.repositories.order_repo import OrderRepo
from app.database.repositories.referral_repo import ReferralRepo
from app.services import settings_service, wallet_service


async def try_qualify_referral(session: AsyncSession, *, user_id: int, referred_by_id: int | None) -> None:
    """Called right after an order completes. Qualifies the referrer on the referee's FIRST
    completed order (rule lives in settings, not code) and credits the reward once — the
    unique constraint on referee_id plus this existence check make it idempotent even if
    called twice for the same user."""
    if referred_by_id is None:
        return

    referral_repo = ReferralRepo(session)
    existing = await referral_repo.get_for_referee(user_id)
    if existing is not None and existing.qualified_at is not None:
        return

    completed_orders = [o for o in await OrderRepo(session).list_for_user(user_id, limit=2) if o.status == OrderStatus.COMPLETED]
    if len(completed_orders) != 1:
        return  # not their first completed order (or none yet)

    reward_minor = await settings_service.get_setting(session, "referral_reward_minor")
    now = datetime.now(UTC)

    if existing is None:
        referral = Referral(referrer_id=referred_by_id, referee_id=user_id, qualified_at=now, reward_minor=reward_minor)
        session.add(referral)
        await session.flush()
    else:
        referral = existing
        referral.qualified_at = now
        referral.reward_minor = reward_minor

    if reward_minor > 0:
        txn = await wallet_service.credit(
            session,
            user_id=referred_by_id,
            amount_minor=reward_minor,
            currency=get_settings().default_currency,
            type_=TxnType.REFERRAL,
            idempotency_key=f"referral:{referral.id}",
            ref_type="referral",
            ref_id=str(referral.id),
        )
        referral.reward_txn_id = txn.id

    await session.flush()
