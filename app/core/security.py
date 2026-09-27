from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets
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


_TAG_RE = re.compile(r"<[^>]*>")
_SPACE_RE = re.compile(r"\s+")


def normalize_stock_text(payload: str) -> str:
    """What makes two stock items "the same login": the text, not how it was formatted.

    Payloads are stored as Telegram HTML, so one login sent once as plain text and once as a
    copy-box would otherwise look like two different items. Tags are dropped, entities decoded and
    every run of whitespace collapsed; case is kept, because passwords are case-sensitive.
    """
    return _SPACE_RE.sub(" ", html.unescape(_TAG_RE.sub("", payload))).strip()


@lru_cache
def _fingerprint_key() -> bytes:
    from app.core.config import get_settings

    return hashlib.sha256(b"stock-fingerprint\x00" + get_settings().encryption_key.encode()).digest()


def stock_fingerprint(payload: str) -> str:
    """A keyed hash of a stock item's normalised text — how the same login is recognised twice.

    Fernet ciphertext is different every time the same text is encrypted, so it cannot be compared;
    this can. Keyed (HMAC with a key derived from ENCRYPTION_KEY) rather than a plain SHA-256,
    because a short password's plain hash can be brute-forced from a database dump.
    """
    return hmac.new(
        _fingerprint_key(), normalize_stock_text(payload).encode(), hashlib.sha256
    ).hexdigest()


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
    "REFUND_FROZEN": "FRZ",
    "REFUND_UNFROZEN": "UFZ",
    "REFUND_SANCTIONED": "SNC",
    "SANCTION_RELEASED": "SRL",
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
