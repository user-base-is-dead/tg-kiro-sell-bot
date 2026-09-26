from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.payments.crypto import CryptoPaymentProcessor

logger = logging.getLogger(__name__)


async def handle_crypto_webhook(
    payload: str, signature: str, session: AsyncSession, bot: Any
) -> bool:
    """
    Handle incoming cryptocurrency payment webhook.

    This function:
    1. Verifies webhook signature to prevent tampering
    2. Processes payment with user isolation
    3. Auto-credits wallet on successful verification
    4. Notifies user of payment status
    """
    processor = CryptoPaymentProcessor()

    # Verify webhook authenticity
    if not processor.verify_webhook_signature(payload, signature):
        logger.error("Invalid webhook signature")
        return False

    try:
        import json

        event_data = json.loads(payload)
    except Exception as e:
        logger.error(f"Failed to parse webhook payload: {e}")
        return False

    # Process payment with user isolation
    success = await processor.process_webhook(session, event_data)

    if success:
        charge_id = event_data.get("charge_id")
        user_id = event_data.get("user_id")

        # Notify user of successful payment
        try:
            message = (
                "✅ <b>Payment received</b>\n\n"
                f"<blockquote>👛 Amount: {event_data.get('received_amount')} {event_data.get('currency')}\n"
                f"➕ Fee: {event_data.get('fee_amount', 'N/A')}\n"
                f"🔗 Transaction: <code>{event_data.get('tx_hash', 'pending')}</code></blockquote>\n\n"
                "Your wallet has been topped up. Thanks for choosing PowerX Digital!"
            )
            await bot.send_message(user_id, message)
        except Exception as e:
            logger.error(f"Failed to notify user {user_id}: {e}")

        return True

    else:
        charge_id = event_data.get("charge_id")
        user_id = event_data.get("user_id")
        received = event_data.get("received_amount", "0")

        # Notify user of payment mismatch
        try:
            message = (
                "⚠️ <b>Amount doesn't match</b>\n\n"
                f"<blockquote>Expected: {event_data.get('expected_amount')} {event_data.get('currency')}\n"
                f"Received: {received} {event_data.get('currency')}</blockquote>\n\n"
                "The amount we received doesn't match the invoice. If you think this is an error, "
                "open a ticket in 🎧 Support and we'll sort it out."
            )
            await bot.send_message(user_id, message)
        except Exception as e:
            logger.error(f"Failed to notify user {user_id} of mismatch: {e}")

        return False
