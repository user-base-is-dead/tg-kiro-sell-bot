from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "OrderRepo.claim_available_stock uses `SELECT ... FOR UPDATE SKIP LOCKED`, which SQLite "
        "doesn't support (no row-level locking) — this test suite runs against an in-memory "
        "SQLite DB with no external services required. Verifying the race-safety guarantee "
        "itself needs a real Postgres instance: fire ~20 concurrent place_order() calls at a "
        "product with 5 AVAILABLE stock_items and assert exactly 5 orders complete, 15 fail "
        "with errors.out_of_stock, and 0 stock_items end up referenced by two orders. Run this "
        "manually against a scratch Postgres before deploying if the claim path changes."
    )
)


async def test_twenty_concurrent_claims_against_five_stock_items_oversell_nothing():
    ...
