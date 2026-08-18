from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.common import back_keyboard
from app.core.config import get_settings
from app.database.models.order import Warranty, WarrantyStatus
from app.database.models.user import User
from app.database.repositories.warranty_repo import WarrantyRepo
from app.locales.i18n import t
from app.services.support_service import active_thread, create_ticket
from app.services.warranty_service import CLAIM_GRACE, format_duration, is_expired, now_utc, open_claim
from app.utils.money import format_minor
from app.utils.time import as_utc

router = Router(name="warranty.claim")

NO_WARRANTY_MSG = "This product does not come with a warranty."


def _claim_subject(warranty: Warranty) -> str:
    """The opening message of the claim thread. Support needs the two warranty timestamps in front
    of them, because whether `/done` pays anything out depends entirely on the expiry."""
    item = warranty.order_item
    order = item.order
    return (
        f"Warranty claim — {item.product_name}\n\n"
        f"Order: {order.order_number}\n"
        f"Price: {format_minor(item.unit_price_minor, order.currency)}\n"
        f"Warranty started: {as_utc(warranty.starts_at):%Y-%m-%d %H:%M} UTC\n"
        f"Warranty expires: {as_utc(warranty.expires_at):%Y-%m-%d %H:%M} UTC\n"
        f"Warranty id: {warranty.id}\n\n"
        f"Resolve with /done {warranty.id} or /reject {warranty.id} [reason]."
    )


@router.callback_query(F.data.startswith("wclaim:"))
async def claim_warranty(query: CallbackQuery, session: AsyncSession, user: User) -> None:
    """File a warranty claim.

    The button is shown for every purchased item, so most of this handler is telling the customer
    why a particular item cannot be claimed. Only an ACTIVE warranty that has not yet passed its
    stored `expires_at` gets a ticket.
    """
    if not query.message:
        return

    warranty_id = int(query.data.split(":")[1])
    repo = WarrantyRepo(session)
    warranty = await repo.get_by_id(warranty_id)

    if warranty is None or warranty.user_id != user.id:
        await query.answer("Warranty not found.", show_alert=True)
        return

    item = warranty.order_item
    if item is not None and item.warranty_days <= 0:
        await query.answer(NO_WARRANTY_MSG, show_alert=True)
        return

    if warranty.status is WarrantyStatus.CLAIMED:
        await query.answer("A claim for this item is already under review.", show_alert=True)
        return

    if warranty.status is not WarrantyStatus.ACTIVE:
        await query.answer("This warranty is no longer active.", show_alert=True)
        return

    now = now_utc()
    # Checked against the stored instant rather than the stored status, because the hourly expiry
    # job may not have run yet — a warranty that lapsed four minutes ago is lapsed, not claimable.
    if is_expired(warranty, now):
        warranty.status = WarrantyStatus.EXPIRED
        await session.flush()
        await query.answer("This warranty has expired.", show_alert=True)
        return

    # Same one-conversation rule the ticket screen enforces, checked from this side too — a claim
    # opened alongside a live ticket would split this person across two threads, and their replies
    # can only be carried into one of them.
    active = await active_thread(session, user.id)
    if active is not None:
        await query.answer(
            t(f"support.blocked_by_{active.kind}", user.locale, reference=active.reference),
            show_alert=True,
        )
        return

    remaining = format_duration(int((as_utc(warranty.expires_at) - now).total_seconds()))

    ticket, reached_staff = await create_ticket(
        query.bot,
        session,
        user=user,
        category="Warranty Claim",
        subject=_claim_subject(warranty),
        support_group_id=get_settings().support_group_id,
    )

    if not reached_staff:
        # Nobody was told, so nothing is under review. Filing the claim anyway would put the
        # warranty into CLAIMED — freezing its countdown display and blocking every other request
        # this user could make — on the strength of a message that reached no one. The warranty is
        # left exactly as it was and they are asked to come back.
        await query.answer(t("support.system_unavailable", user.locale), show_alert=True)
        return

    open_claim(warranty, ticket_id=ticket.id, at=now)
    await session.flush()

    grace_hours = int(CLAIM_GRACE.total_seconds() // 3600)
    delivery_note = (
        "⏱️ Our team will respond within "
        f"{grace_hours} hours. If nobody responds in time the claim is closed automatically and "
        "your warranty simply carries on from where it stands."
    )

    text = (
        "✅ <b>Warranty Claim Submitted</b>\n\n"
        f"🎫 Ticket: <code>{ticket.ticket_number}</code>\n"
        f"📦 Product: {warranty.order_item.product_name}\n"
        f"⏱️ Warranty time held for you: <b>{remaining}</b>\n"
        f"⏳ Original expiry: {as_utc(warranty.expires_at):%d %b %Y %H:%M} UTC\n"
        "📋 Status: Under review\n\n"
        f"{delivery_note}\n\n"
        "ℹ️ If we replace the item, the time above is added to the replacement from the moment it's "
        "handed over. Note that the original warranty period keeps running while we review, so "
        "please don't wait if it's close to expiring."
    )

    await query.message.edit_text(text, reply_markup=back_keyboard(user.locale))
    await query.answer()
