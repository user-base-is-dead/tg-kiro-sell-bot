from __future__ import annotations

import hashlib
import secrets
import string
from functools import lru_cache

from cryptography.fernet import Fernet


class PayloadCipher:
    """Encrypts stock payloads at rest (AES-128-CBC + HMAC via Fernet) so a DB dump alone
    never leaks sellable goods."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()


@lru_cache
def get_cipher() -> PayloadCipher:
    from app.core.config import get_settings

    return PayloadCipher(get_settings().encryption_key)


def new_idempotency_key() -> str:
    return secrets.token_urlsafe(16)


def new_order_number() -> str:
    return "ORD-" + secrets.token_hex(3).upper()


def new_ticket_number() -> str:
    return "TCK-" + secrets.token_hex(3).upper()


# The prefix each kind of order event wears. It is part of the identifier rather than a column you
# have to look up, so an ID quoted on its own — in a refund conversation, in a screenshot, pasted
# into the admin search bar — already says what kind of thing it refers to.
EVENT_PREFIXES: dict[str, str] = {
    "PLACED": "PLC",
    "DELIVERED": "DLV",
    "DECLINED": "DEC",
    "REFUND_PARKED": "RFD",
    "REFUND_PAID_OUT": "PAY",
    "REFUND_MOVED": "MOV",
    "TICKET_OPENED": "TKT",
}


def new_event_number(kind: str) -> str:
    """`DEC-1A0F73` — the searchable handle for one order event.

    Four hex bytes rather than the three an order number gets: events outnumber orders several to
    one, and a collision here is a lost audit line rather than a retryable checkout. An unknown kind
    falls back to `EVT` instead of raising — a missing prefix is a cosmetic problem, and refusing to
    mint an ID would take down the decline it was recording.
    """
    return f"{EVENT_PREFIXES.get(kind, 'EVT')}-" + secrets.token_hex(3).upper()


_GIFT_ALPHABET = string.ascii_uppercase + string.digits


def new_gift_code() -> str:
    return "".join(secrets.choice(_GIFT_ALPHABET) for _ in range(12))


def hash_gift_code(code: str) -> str:
    """Codes are stored hashed (like passwords) — a DB dump doesn't hand out redeemable
    codes. Only the last 4 characters are kept in the clear, for admin identification."""
    return hashlib.sha256(code.encode()).hexdigest()
