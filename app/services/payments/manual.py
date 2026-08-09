from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import wallet_service
from app.services.payments.provider import PaymentIntent, PaymentProvider


class ManualPaymentProvider(PaymentProvider):
    """User submits proof of an out-of-band payment; an admin reviews and approves/rejects.
    Fully working end-to-end — adding Stripe/crypto later doesn't touch this file."""

    id = "manual"
    display_name = "Manual Top-up (admin approval)"

    async def is_enabled(self) -> bool:
        return True

    async def create_intent(
        self, session: AsyncSession, *, user_id: int, amount_minor: int, currency: str, proof: str | None = None
    ) -> PaymentIntent:
        txn = await wallet_service.create_pending_topup(
            session, user_id=user_id, amount_minor=amount_minor, currency=currency, proof=proof or ""
        )
        return PaymentIntent(transaction_id=txn.id, amount_minor=amount_minor, currency=currency)

    def render_instructions(self, intent: PaymentIntent) -> str:
        return (
            "⏳ Your top-up request has been submitted for review.\n"
            "An admin will approve it shortly and your balance will update automatically."
        )

    async def status(self, session: AsyncSession, intent: PaymentIntent) -> str:
        from app.database.models.wallet import WalletTransaction

        txn = await session.get(WalletTransaction, intent.transaction_id)
        if txn is None:
            return "FAILED"
        return {"PENDING": "PENDING", "COMPLETED": "PAID", "FAILED": "FAILED"}[txn.status.value]
