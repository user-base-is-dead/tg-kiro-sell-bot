"""Leaving the payment screen cancels the payment.

While a buyer has an open USDT checkout invoice, doing anything in the bot other than the three things
the payment screen is for — 📋 Copy amount and 📋 Copy address (handled by Telegram itself, so the bot
never even sees them) and 🔄 Check Payment — ends it: the invoice is CANCELLED, the held item goes
straight back on sale, and the buyer is told. ✖️ Cancel does the same thing itself.

What does NOT end it:
  * anything the bot sends on its own — broadcasts, announcements, support replies. Those are not
    updates from the buyer, so they never pass through here at all;
  * the buyer chatting with support in an open ticket: a plain message is support talk, not a way off
    the payment screen. With no ticket open, the bot doesn't answer a plain message at all, so that
    isn't a way off either.

What does: every other button, every /command (/start included), every reply-keyboard menu press, and
a typed answer to some other question the bot is waiting on.

Money is never at risk from this: a transfer that arrives for a cancelled invoice is still matched
and credited to the buyer's wallet (see app/jobs/crypto_payment_checker.py).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import TelegramObject, Update

from app.bot.filters.menu_button import menu_labels
from app.bot.states.ticket_form import TicketForm
from app.database.repositories.product_repo import ProductRepo
from app.locales.i18n import _load
from app.services.payments import invoices
from app.utils.text import escape_html

logger = logging.getLogger(__name__)

# The buttons that belong to the payment screen itself, plus the empty filler button.
_KEEPS_PAYMENT = ("check_topup_crypto:", "cancel_topup_crypto:")


@lru_cache
def _menu_texts(locale: str) -> frozenset[str]:
    return frozenset(
        label for key in _load(locale).get("menu", {}) for label in menu_labels(f"menu.{key}", locale)
    )


def leaves_payment(event: Update, locale: str, raw_state: str | None) -> bool:
    """Whether this update is the buyer doing something other than paying."""
    if event.callback_query is not None:
        query = event.callback_query
        if query.message is None or query.message.chat.type != "private":
            return False
        data = query.data or ""
        return data != "noop" and not data.startswith(_KEEPS_PAYMENT)

    message = event.message
    if message is None or message.chat.type != "private":
        return False
    text = (message.text or "").strip()
    if text.startswith("/"):
        return True
    if text and text in _menu_texts(locale):
        return True
    # Typing into some other form the bot is waiting on (a quantity, a stock item, …) gets a new
    # screen back. Typing into the ticket form is talking to support.
    return raw_state is not None and not raw_state.startswith(f"{TicketForm.__name__}:")


class PendingPaymentMiddleware(BaseMiddleware):
    """Registered after UserMiddleware (it needs `user`) and after the session is opened."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("user")
        session = data.get("session")
        if (
            not isinstance(event, Update)
            or user is None
            or session is None
            or not leaves_payment(event, user.locale, data.get("raw_state"))
        ):
            return await handler(event, data)

        # Before the handler, so whatever the buyer asked for next starts from a clean slate — a
        # second 💎 Pay with USDT gets its item back first and then holds it again.
        cancelled = await invoices.cancel_live_invoice(session, user.id)
        if cancelled is None:
            return await handler(event, data)

        # Committed on its own, before the handler runs: a handler that fails — or rolls back on
        # purpose, like a wallet confirm that comes up short — must not quietly bring the invoice
        # back to life after the buyer has been told it is gone.
        name = await self._product_name(session, cancelled)
        await session.commit()
        chat = event.callback_query.message.chat if event.callback_query is not None else event.message.chat
        try:
            return await handler(event, data)
        finally:
            await self._tell(data.get("bot"), chat.id, name)

    @staticmethod
    async def _product_name(session, cancelled) -> str:
        product_id = invoices.invoice_product_id(cancelled)
        product = await ProductRepo(session).get_by_id(product_id) if product_id is not None else None
        return escape_html(product.name) if product is not None else "your item"

    @staticmethod
    async def _tell(bot, chat_id: int, name: str) -> None:
        if bot is None:
            return
        try:
            await bot.send_message(
                chat_id,
                "✖️ <b>USDT payment cancelled</b>\n\n"
                f"You left the payment screen, so the payment for <b>{name}</b> was cancelled and "
                "it's back on sale.\n\n"
                "Already sent the money? It isn't lost — once it arrives it's added to your wallet "
                "balance.",
            )
        except TelegramAPIError as exc:
            logger.warning("Couldn't tell user %s their payment was cancelled (%s)", chat_id, exc)
