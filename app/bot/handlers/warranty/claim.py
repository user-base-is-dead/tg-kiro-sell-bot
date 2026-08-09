from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import NavCB
from app.bot.keyboards.common import back_keyboard
from app.database.models.order import WarrantyStatus
from app.database.models.wallet import TxnType
from app.database.repositories.warranty_repo import WarrantyRepo
from app.database.repositories.support_repo import SupportRepo
from app.database.models.user import User
from app.locales.i18n import t
from app.utils.money import format_minor

router = Router(name="warranty.claim")


async def _create_warranty_claim_ticket(
    session: AsyncSession, warranty_id: int, user_id: int, user: User
) -> int | None:
    """Creates a support ticket for warranty claim with all product details."""
    warranty_repo = WarrantyRepo(session)
    warranty = await warranty_repo.get_by_id(warranty_id)
    if warranty is None:
        return None

    support_repo = SupportRepo(session)

    subject = f"Warranty Claim - Order #{warranty.order_item.order.order_number}"
    body = (
        f"Product: {warranty.order_item.product_name}\n"
        f"Price: {format_minor(warranty.order_item.unit_price_minor, warranty.order_item.order.currency)}\n"
        f"Warranty Start: {warranty.starts_at.strftime('%Y-%m-%d %H:%M')}\n"
        f"Warranty Expires: {warranty.expires_at.strftime('%Y-%m-%d %H:%M')}\n"
        f"Purchase Date: {warranty.order_item.order.placed_at.strftime('%Y-%m-%d %H:%M') if warranty.order_item.order.placed_at else 'N/A'}\n\n"
        f"User: {user.first_name or 'N/A'}\n"
        f"Username: @{user.username or 'N/A'}"
    )

    now = datetime.now(UTC)
    ticket = await support_repo.create_ticket(
        referrer_user_id=user_id,
        subject=subject,
        category="Warranty Claim",
        support_group_id=0,
        reached_staff=False,
    )

    return ticket.id if ticket else None


@router.callback_query(NavCB.filter(F.target == "claim_warranty"))
async def claim_warranty(query: CallbackQuery, callback_data: NavCB, session: AsyncSession, user: User) -> None:
    """Handle warranty claim request."""
    if not query.message:
        return

    warranty_id = int(callback_data.data or 0) if callback_data.data else 0
    if warranty_id == 0:
        await query.answer("Invalid warranty.", show_alert=True)
        return

    warranty_repo = WarrantyRepo(session)
    warranty = await warranty_repo.get_by_id(warranty_id)

    if warranty is None or warranty.status != "ACTIVE":
        await query.answer("This warranty is no longer active.", show_alert=True)
        return

    now = datetime.now(UTC)
    if now > warranty.expires_at:
        warranty.status = WarrantyStatus.EXPIRED
        await session.flush()
        await query.answer("Warranty period has expired.", show_alert=True)
        return

    ticket_id = await _create_warranty_claim_ticket(session, warranty.id, user.id, user)
    if ticket_id is None:
        await query.answer("Failed to create claim. Try again later.", show_alert=True)
        return

    warranty.status = WarrantyStatus.CLAIMED
    warranty.claim_started_at = now
    warranty.claim_ticket_id = ticket_id
    await session.flush()

    text = (
        "✅ <b>Warranty Claim Submitted</b>\n\n"
        "Your warranty claim has been received and assigned to our support team.\n\n"
        "📋 <b>Claim Details:</b>\n"
        f"Product: {warranty.order_item.product_name}\n"
        f"Warranty expires: {warranty.expires_at.strftime('%Y-%m-%d %H:%M')}\n"
        f"Status: Pending Review\n\n"
        "⏱️ You will receive a response within 24 hours.\n"
        "If no response is received within 24 hours, your claim will be auto-rejected. "
        "In that case, please contact Customer Support."
    )

    await query.message.edit_text(text, reply_markup=back_keyboard(user.locale))
    await query.answer()
