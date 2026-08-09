from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters.is_admin import IsAdmin
from app.database.models.order import WarrantyStatus
from app.database.repositories.warranty_repo import WarrantyRepo
from app.database.repositories.support_repo import SupportRepo
from app.database.models.user import User
from app.utils.errors import UserError

router = Router(name="admin.warranty_claims")
router.message.filter(IsAdmin())


async def _get_warranty_from_ticket(session: AsyncSession, ticket_id: int):
    """Get warranty associated with a ticket."""
    warranty_repo = WarrantyRepo(session)
    warranty = await warranty_repo.get_by_ticket_id(ticket_id)
    return warranty


@router.message(Command("done"))
async def approve_warranty_claim(message: Message, session: AsyncSession, user: User) -> None:
    """Admin approves warranty claim: /done <warranty_id>"""
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /done <warranty_id>")
        return

    try:
        warranty_id = int(args[1])
    except ValueError:
        await message.answer("Invalid warranty ID.")
        return

    warranty_repo = WarrantyRepo(session)
    warranty = await warranty_repo.get_by_id(warranty_id)

    if warranty is None:
        await message.answer("Warranty not found.")
        return

    if warranty.status != WarrantyStatus.CLAIMED:
        await message.answer("Warranty is not in claimed state.")
        return

    now = datetime.now(UTC)
    time_used = (now - warranty.claim_started_at).total_seconds() if warranty.claim_started_at else 0
    warranty_duration = (warranty.expires_at - warranty.starts_at).total_seconds()
    remaining_seconds = max(0, warranty_duration - time_used)

    warranty.status = WarrantyStatus.ACTIVE
    warranty.expires_at = now + (remaining_seconds // 1)
    await session.flush()

    bot_message = (
        f"✅ Your warranty claim has been <b>APPROVED</b>.\n\n"
        f"Remaining warranty time: {int(remaining_seconds // 3600)} hours\n\n"
        f"If you need further assistance, please contact our Customer Support."
    )

    if warranty.order_item.order.user_id:
        try:
            await message.bot.send_message(warranty.order_item.order.user_id, bot_message)
        except Exception:
            pass

    await message.answer(f"✅ Warranty claim #{warranty_id} approved.")


@router.message(Command("reject"))
async def reject_warranty_claim(message: Message, session: AsyncSession, user: User) -> None:
    """Admin rejects warranty claim: /reject <warranty_id> [reason]"""
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.answer("Usage: /reject <warranty_id> [reason]")
        return

    try:
        warranty_id = int(args[1])
    except ValueError:
        await message.answer("Invalid warranty ID.")
        return

    reason = args[2] if len(args) > 2 else "Warranty claim cannot be processed."

    warranty_repo = WarrantyRepo(session)
    warranty = await warranty_repo.get_by_id(warranty_id)

    if warranty is None:
        await message.answer("Warranty not found.")
        return

    if warranty.status != WarrantyStatus.CLAIMED:
        await message.answer("Warranty is not in claimed state.")
        return

    warranty.status = WarrantyStatus.VOID
    warranty.claim_notes = reason
    await session.flush()

    bot_message = (
        f"❌ Your warranty claim has been <b>REJECTED</b>.\n\n"
        f"Reason: {reason}\n\n"
        f"If you believe this is incorrect, please contact our Customer Support team."
    )

    if warranty.order_item.order.user_id:
        try:
            await message.bot.send_message(warranty.order_item.order.user_id, bot_message)
        except Exception:
            pass

    await message.answer(f"✅ Warranty claim #{warranty_id} rejected.")
