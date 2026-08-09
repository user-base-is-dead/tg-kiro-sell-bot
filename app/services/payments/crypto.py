from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.models.wallet import TxnType
from app.database.repositories.user_repo import UserRepo
from app.services import wallet_service
from app.utils.errors import UserError

logger = logging.getLogger(__name__)


class CryptoPaymentProcessor:
    """Handles cryptocurrency payments with auto-verification and fee tracking."""

    def __init__(self):
        self.settings = get_settings()
        self.webhook_secret = self.settings.crypto_webhook_secret
        self.fee_percentage = Decimal("0.01")  # 1% fee
        self.min_difference = Decimal("0.1")  # Max 0.1 difference allowed

    async def create_charge(
        self,
        session: AsyncSession,
        user_id: int,
        amount_minor: int,
        currency: str,
        description: str,
    ) -> dict:
        """Create a crypto charge that user sends payment to."""
        from app.database.models.crypto import CryptoPayment

        user = await UserRepo(session).get_by_id(user_id)
        if user is None:
            raise UserError("errors.user_not_found")

        # Calculate what we expect to receive (with fee)
        base_amount = Decimal(amount_minor) / 100
        expected_amount = base_amount / (1 - self.fee_percentage)

        payment = CryptoPayment(
            user_id=user_id,
            product_amount_minor=amount_minor,
            expected_amount=str(expected_amount),
            currency=currency,
            description=description,
            status="PENDING",
            created_at=datetime.now(UTC),
        )
        session.add(payment)
        await session.flush()

        return {
            "charge_id": str(payment.id),
            "user_id": user_id,
            "expected_amount": str(expected_amount),
            "currency": currency,
            "description": description,
            "product_amount": str(base_amount),
            "fee_percentage": str(self.fee_percentage * 100),
            "payment_address": f"https://payment.example.com/crypto/{payment.id}",
        }

    def verify_webhook_signature(self, payload: str, signature: str) -> bool:
        """Verify webhook signature to prevent tampering."""
        expected = hmac.new(
            self.webhook_secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def process_webhook(
        self, session: AsyncSession, event_data: dict
    ) -> bool:
        """Process incoming crypto payment webhook with user isolation."""
        from app.database.models.crypto import CryptoPayment

        charge_id = event_data.get("charge_id")
        received_amount = Decimal(event_data.get("received_amount", "0"))
        tx_hash = event_data.get("tx_hash")

        # Fetch payment with user isolation
        payment = await session.get(CryptoPayment, int(charge_id))
        if payment is None:
            logger.error(f"Payment {charge_id} not found")
            return False

        # Check if already processed (prevents duplicate credits)
        if payment.status in ("CONFIRMED", "COMPLETED"):
            logger.warning(f"Payment {charge_id} already processed")
            return False

        # Expected amount to receive
        expected = Decimal(payment.expected_amount)
        difference = abs(received_amount - expected)

        # Validate amount (must be within tolerance)
        if difference > self.min_difference:
            payment.status = "MISMATCH"
            payment.actual_amount = str(received_amount)
            payment.tx_hash = tx_hash
            await session.flush()
            logger.error(
                f"Payment {charge_id}: expected {expected}, got {received_amount}"
            )
            return False

        # Calculate actual product amount and fee
        fee_amount = received_amount - Decimal(payment.product_amount_minor) / 100
        actual_product_amount = int(
            (Decimal(payment.product_amount_minor) / 100) * 100
        )

        # User isolation: ensure credit goes to correct user only
        user = await UserRepo(session).get_by_id(payment.user_id)
        if user is None:
            logger.error(f"User {payment.user_id} not found for payment {charge_id}")
            return False

        # Credit user's wallet (isolated to this user only)
        txn = await wallet_service.credit(
            session,
            user_id=payment.user_id,
            amount_minor=actual_product_amount,
            currency=payment.currency,
            type_=TxnType.PURCHASE,
            idempotency_key=f"crypto:{charge_id}:{tx_hash}",
            ref_type="crypto_payment",
            ref_id=str(payment.id),
        )

        # Mark payment as confirmed
        payment.status = "CONFIRMED"
        payment.actual_amount = str(received_amount)
        payment.fee_amount = str(fee_amount)
        payment.tx_hash = tx_hash
        payment.wallet_transaction_id = txn.id
        payment.confirmed_at = datetime.now(UTC)
        await session.flush()

        logger.info(
            f"Payment {charge_id} confirmed for user {payment.user_id}: "
            f"{received_amount} {payment.currency} (fee: {fee_amount})"
        )
        return True

    async def get_payment_status(
        self, session: AsyncSession, charge_id: str, user_id: int
    ) -> dict:
        """Get payment status with user isolation."""
        from app.database.models.crypto import CryptoPayment

        payment = await session.get(CryptoPayment, int(charge_id))
        if payment is None or payment.user_id != user_id:
            raise UserError("common.unknown_action")

        return {
            "charge_id": charge_id,
            "status": payment.status,
            "product_amount": str(Decimal(payment.product_amount_minor) / 100),
            "expected_amount": payment.expected_amount,
            "actual_amount": payment.actual_amount,
            "fee": payment.fee_amount,
            "tx_hash": payment.tx_hash,
            "created_at": payment.created_at.isoformat() if payment.created_at else None,
            "confirmed_at": payment.confirmed_at.isoformat()
            if payment.confirmed_at
            else None,
        }
