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

from app.database.models.order import OrderStatus
from app.locales.i18n import t
from app.services import announcement_service, order_service, order_thread_service
from app.utils.text import pack_messages


_PAYLOAD_SLOT = "\x00"


async def buyer_messages(session: AsyncSession, placed, locale: str, heading: str = "") -> list[str]:
    """The buyer's receipt — and for AUTO, their goods — as one message or, if it is long, several.

    Several because Telegram refuses anything over 4096 characters outright: three longer logins in
    one message used to be rejected whole, after the order had been paid and marked delivered, so
    the buyer was charged and received nothing. Every message after the first carries more of the
    credentials; no credential is ever split across two.
    """
    from app.bot.delivery_notes import delivery_note

    order = placed.order
    head = heading + t("orders.placed", locale, order_number=order.order_number) + "\n\n"
    note = await delivery_note(session, placed.order_item.product_id, locale)
    tail = f"\n\n{note}" if note else ""

    if not placed.delivered_payloads:
        return [head + t("orders.manual_pending", locale) + tail]

    # The template is split around the payload so the credentials can flow over as many messages
    # as they need while its heading and warranty line stay where they belong.
    template = t(
        "orders.auto_delivery",
        locale,
        payload=_PAYLOAD_SLOT,
        warranty_days=placed.order_item.warranty_days,
    )
    before, after = template.split(_PAYLOAD_SLOT, 1)
    # Numbered when there is more than one, so a buyer who ordered five can tell at a glance that
    # five arrived and which line is which.
    payloads = placed.delivered_payloads
    items = (
        list(payloads)
        if len(payloads) == 1
        else [f"{i}. {p}" for i, p in enumerate(payloads, start=1)]
    )
    return pack_messages(head + before, items, after + tail)


async def after_purchase(bot: Bot, session: AsyncSession, placed) -> None:
    """The side effects of a sale. Each is best-effort on its own terms: the sale is already done."""
    order = placed.order
    # On the order's status, not on whether payloads came back: a replayed Confirm used to arrive
    # with none and put an already-delivered order in front of staff as awaiting fulfilment.
    if order.status is OrderStatus.PROCESSING:
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
