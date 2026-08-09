from __future__ import annotations

import os

os.environ.setdefault("BOT_TOKEN", "123456:test-token-not-real-0000000000000")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from cryptography.fernet import Fernet  # noqa: E402

from app.core.security import PayloadCipher, hash_gift_code, new_gift_code, new_idempotency_key, new_order_number  # noqa: E402


def test_payload_cipher_round_trip():
    cipher = PayloadCipher(Fernet.generate_key().decode())
    plaintext = "SUPER-SECRET-LICENSE-KEY-123"
    ciphertext = cipher.encrypt(plaintext)

    assert plaintext not in ciphertext
    assert cipher.decrypt(ciphertext) == plaintext


def test_gift_code_is_reasonable_length_and_alphabet():
    code = new_gift_code()
    assert len(code) == 12
    assert code.isalnum()
    assert code == code.upper()


def test_gift_code_hash_is_deterministic_and_one_way():
    code = new_gift_code()
    h1 = hash_gift_code(code)
    h2 = hash_gift_code(code)
    assert h1 == h2
    assert code not in h1


def test_order_number_format():
    n = new_order_number()
    assert n.startswith("ORD-")
    assert len(n) == len("ORD-") + 6


def test_idempotency_keys_are_unique():
    keys = {new_idempotency_key() for _ in range(1000)}
    assert len(keys) == 1000
