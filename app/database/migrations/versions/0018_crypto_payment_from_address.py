"""Remember which address a confirmed crypto payment came from.

The chain carries no order id, so a transfer is matched to an invoice by amount. That works right up
until two buyers owe the same amount at the same time, and then the checker refuses to guess — it
logs "ambiguous" and credits neither, which is correct but leaves both waiting.

The sending address is the one other identifying thing a transfer carries. Recorded here, a buyer's
second payment can be attributed to them even when the amount alone is ambiguous. It is deliberately
only a tiebreaker: withdrawals from an exchange come out of a shared hot wallet, so an address seen
paying for more than one account proves nothing and stops being used.

Existing rows get NULL — no history to backfill, and the tiebreaker simply has nothing to go on for
buyers who have not paid since.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-18

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("crypto_payments") as batch:
        batch.add_column(sa.Column("from_address", sa.String(length=64), nullable=True))
        batch.create_index("ix_crypto_payments_from_address", ["from_address"])


def downgrade() -> None:
    with op.batch_alter_table("crypto_payments") as batch:
        batch.drop_index("ix_crypto_payments_from_address")
        batch.drop_column("from_address")
