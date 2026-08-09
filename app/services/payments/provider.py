from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class PaymentIntent:
    transaction_id: int
    amount_minor: int
    currency: str


class PaymentProvider(ABC):
    """The core never imports a concrete provider directly — it goes through the registry.
    Adding Stripe/crypto later is one new file here + a registry entry, zero core changes."""

    id: str
    display_name: str

    @abstractmethod
    async def is_enabled(self) -> bool: ...

    @abstractmethod
    async def create_intent(
        self, session: AsyncSession, *, user_id: int, amount_minor: int, currency: str, proof: str | None = None
    ) -> PaymentIntent: ...

    @abstractmethod
    def render_instructions(self, intent: PaymentIntent) -> str: ...

    @abstractmethod
    async def status(self, session: AsyncSession, intent: PaymentIntent) -> str:  # PENDING | PAID | FAILED
        ...
