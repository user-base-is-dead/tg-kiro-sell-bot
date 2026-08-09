from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.order import WarrantyStatus
from app.database.repositories.warranty_repo import WarrantyRepo

logger = logging.getLogger(__name__)


async def auto_reject_expired_warranty_claims(session: AsyncSession, bot) -> None:
    """Auto-reject warranty claims that haven't been responded to within 24 hours."""
    warranty_repo = WarrantyRepo(session)
    pending = await warranty_repo.list_pending_claims()

    for warranty in pending:
        if warranty.status != WarrantyStatus.CLAIMED or warranty.claim_started_at is None:
            continue

        now = datetime.now(UTC)
        claim_age = now - warranty.claim_started_at

        if claim_age >= timedelta(hours=24):
            warranty.status = WarrantyStatus.VOID
            warranty.claim_notes = "Auto-rejected: No response from admin within 24 hours."

            message = (
                "❌ Your warranty claim has been <b>AUTO-REJECTED</b> due to no response within 24 hours.\n\n"
                "If you believe this is incorrect, please contact our Customer Support team."
            )

            try:
                if warranty.order_item.order.user_id:
                    await bot.send_message(warranty.order_item.order.user_id, message)
            except Exception as e:
                logger.error(f"Failed to send auto-reject message: {e}")

            logger.info(f"Auto-rejected warranty claim {warranty.id}")

    await session.flush()
