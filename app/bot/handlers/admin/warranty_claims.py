from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters.is_admin import IsAdmin
from app.database.models.order import Warranty, WarrantyStatus
from app.database.models.user import User
from app.database.repositories.user_repo import UserRepo
from app.database.repositories.warranty_repo import WarrantyRepo
from app.services.support_service import close_claim_ticket
from app.services.warranty_service import format_duration, now_utc, reject_claim, resolve_claim
from app.utils.time import as_utc

logger = logging.getLogger(__name__)

router = Router(name="admin.warranty_claims")
router.message.filter(IsAdmin())


async def _notify_customer(message: Message, session: AsyncSession, warranty: Warranty, text: str) -> None:
    """Best-effort DM to the warranty's owner. A blocked bot must not roll back the resolution the
    admin just performed, so failures are logged and swallowed."""
    owner = await UserRepo(session).get_by_id(warranty.user_id)
    if owner is None:
        logger.warning("Warranty %s has no owner row to notify", warranty.id)
        return
    try:
        await message.bot.send_message(owner.telegram_id, text)
    except TelegramAPIError as exc:
        logger.warning("Couldn't notify user %s about warranty %s (%s)", owner.telegram_id, warranty.id, exc)


async def _load_claimed(message: Message, session: AsyncSession, raw_id: str) -> Warranty | None:
    try:
        warranty_id = int(raw_id)
    except ValueError:
        await message.answer("Invalid warranty ID.")
        return None

    warranty = await WarrantyRepo(session).get_by_id(warranty_id)
    if warranty is None:
        await message.answer("Warranty not found.")
        return None
    if warranty.status is not WarrantyStatus.CLAIMED:
        await message.answer(f"Warranty #{warranty_id} is not under claim (status: {warranty.status.value}).")
        return None
    return warranty


@router.message(Command("done"))
async def approve_warranty_claim(message: Message, session: AsyncSession, user: User) -> None:  # noqa: ARG001
    """`/done <warranty_id>` — the claim was resolved, hand the held warranty time to the replacement.

    The grant runs from now, not from the original purchase: the customer lost the use of the item
    while it was being replaced, so the frozen remainder is re-based onto the resolution time.
    """
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Usage: /done &lt;warranty_id&gt;")
        return

    warranty = await _load_claimed(message, session, args[1])
    if warranty is None:
        return

    now = now_utc()
    outcome = resolve_claim(warranty, at=now)
    await session.flush()
    # The claim's own thread ends with the claim — staff should never need a second /close for it.
    await close_claim_ticket(
        message.bot, session, ticket_id=warranty.claim_ticket_id, reason=f"Warranty claim #{warranty.id} resolved"
    )

    if outcome.granted_seconds <= 0:
        # The original warranty ran out while the claim sat in review. Resolving the claim does not
        # conjure time back — see the `expires_at` rule in services/warranty_service.
        await _notify_customer(
            message,
            session,
            warranty,
            "✅ Your warranty claim has been <b>RESOLVED</b>.\n\n"
            "The original warranty period had already ended by the time it was resolved, so no "
            "further warranty time applies to this item.\n\n"
            "Need anything else? Contact Customer Support.",
        )
        await message.answer(
            f"✅ Claim #{warranty.id} resolved. Original warranty had already expired — 0 time granted."
        )
        return

    granted = format_duration(outcome.granted_seconds)
    await _notify_customer(
        message,
        session,
        warranty,
        "✅ Your warranty claim has been <b>RESOLVED</b>.\n\n"
        f"⏱️ Warranty restored: <b>{granted}</b>\n"
        f"⏳ New expiry: {as_utc(warranty.expires_at):%d %b %Y %H:%M} UTC\n\n"
        "The unused time from your original warranty now covers the replacement.\n"
        "If you need further assistance, contact Customer Support.",
    )
    await message.answer(
        f"✅ Claim #{warranty.id} resolved. Granted {granted}, new expiry "
        f"{as_utc(warranty.expires_at):%Y-%m-%d %H:%M} UTC."
    )


@router.message(Command("reject"))
async def reject_warranty_claim(message: Message, session: AsyncSession, user: User) -> None:  # noqa: ARG001
    """`/reject <warranty_id> [reason]` — drop the claim, back to the original warranty timeline.

    Nothing is restarted or extended. Whatever `expires_at` said at purchase still stands, so a
    warranty that outlived the review resumes with the time it genuinely has left, and one that
    lapsed during the review is expired.
    """
    args = (message.text or "").split(maxsplit=2)
    if len(args) < 2:
        await message.answer("Usage: /reject &lt;warranty_id&gt; [reason]")
        return

    reason = args[2].strip() if len(args) > 2 else "Warranty claim could not be approved."

    warranty = await _load_claimed(message, session, args[1])
    if warranty is None:
        return

    outcome = reject_claim(warranty, reason=reason, at=now_utc())
    await session.flush()
    await close_claim_ticket(
        message.bot, session, ticket_id=warranty.claim_ticket_id, reason=f"Warranty claim #{warranty.id} rejected"
    )

    if outcome.granted_seconds <= 0:
        await _notify_customer(
            message,
            session,
            warranty,
            "❌ Your warranty claim has been <b>REJECTED</b>.\n\n"
            f"Reason: {reason}\n\n"
            "The warranty period for this item has also ended.\n"
            "If you believe this is incorrect, contact Customer Support.",
        )
        await message.answer(f"✅ Claim #{warranty.id} rejected. Warranty had already expired.")
        return

    left = format_duration(outcome.granted_seconds)
    await _notify_customer(
        message,
        session,
        warranty,
        "❌ Your warranty claim has been <b>REJECTED</b>.\n\n"
        f"Reason: {reason}\n\n"
        f"⏱️ Your warranty is still active with <b>{left}</b> remaining "
        f"(expires {as_utc(warranty.expires_at):%d %b %Y %H:%M} UTC).\n"
        "If you believe this is incorrect, contact Customer Support.",
    )
    await message.answer(f"✅ Claim #{warranty.id} rejected. Warranty stays active, {left} remaining.")
