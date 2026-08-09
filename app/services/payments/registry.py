from __future__ import annotations

from functools import lru_cache

from app.services.payments.manual import ManualPaymentProvider
from app.services.payments.provider import PaymentProvider

_PROVIDERS: dict[str, PaymentProvider] = {
    "manual": ManualPaymentProvider(),
}


def get_provider(provider_id: str = "manual") -> PaymentProvider:
    return _PROVIDERS[provider_id]


@lru_cache
def default_provider_id() -> str:
    return "manual"
