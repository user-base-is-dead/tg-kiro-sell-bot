"""What happens around a completed purchase, however it was paid for.

A sale can now finish in two places: the buyer pressing ✅ Confirm (wallet), and the payment checker
the moment a USDT transfer arrives (no button at all). Both must hand the buyer the same message and
set off the same side effects, so they live here once:

  * the buyer's message — order confirmed, the items (AUTO) or "being prepared" (MANUAL), and the
    product's delivery note;
  * the orders group — every order gets its topic and card, AUTO ones included, so staff see every
    sale and not only the ones waiting on them;
  * a MANUAL order is put in front of staff with its Fulfil/Decline buttons;
  * a purchase that emptied the shelf is announced.
"""

from __future__ import annotations

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.locales.i18n import t
from app.services import announcement_service, order_service, order_thread_service


async def buyer_message(session: AsyncSession, placed, locale: str) -> str:
    from app.bot.delivery_notes import delivery_note

    order = placed.order
    lines = [t("orders.placed", locale, order_number=order.order_number)]
    if placed.delivered_payloads:
        warranty_days = placed.order_item.warranty_days
        # Numbered when there is more than one, so a buyer who ordered five can tell at a glance that
        # five arrived and which line is which.
        payload = (
            placed.delivered_payloads[0]
            if len(placed.delivered_payloads) == 1
            else "\n".join(f"{i}. {p}" for i, p in enumerate(placed.delivered_payloads, start=1))
        )
        lines.append(t("orders.auto_delivery", locale, payload=payload, warranty_days=warranty_days))
    else:
        lines.append(t("orders.manual_pending", locale))

    note = await delivery_note(session, placed.order_item.product_id, locale)
    if note:
        lines.append(note)
    return "\n\n".join(lines)


async def after_purchase(bot: Bot, session: AsyncSession, placed) -> None:
    """The side effects of a sale. Each is best-effort on its own terms: the sale is already done."""
    order = placed.order
    if not placed.delivered_payloads:
        # Nothing about a manual order reaches an admin on its own — push it, or it waits for
        # somebody to open the admin panel out of curiosity. This also opens its topic.
        await order_service.notify_admins_of_manual_order(bot, session, order)

    # After the buyer has been served, never before: if this purchase emptied the shelf, everyone else
    # hears about it. No admin approval — the event already happened.
    await announcement_service.maybe_announce_sold_out(bot, session, placed.order_item.product_id)

    # Every order gets its own topic in the orders group with its card and history — an AUTO order as
    # much as a MANUAL one. A delivered order's topic is posted and then closed, so the group still
    # reads as a queue of live work while keeping a record of every sale.
    await order_thread_service.sync(bot, session, order)
