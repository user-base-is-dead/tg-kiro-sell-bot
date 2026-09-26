"""Add the Frozen Wallet: a third balance for declined-order money held for review.

When staff decline an order they now choose where its money goes — the Refund Wallet as before, or
the Frozen Wallet, where the buyer can see it but it can be neither spent nor refunded until an
admin releases it into the Refund Wallet.

`wallets.frozen_balance_minor` is that balance; every existing wallet starts at 0. The enums gain the
values the new flow writes:

  txn_type          FROZEN_PARK, FROZEN_RELEASE
  txn_account       FROZEN
  refund_state      FROZEN
  order_event_kind  REFUND_FROZEN, REFUND_UNFROZEN

Only PostgreSQL stores those as real enum types that need extending. SQLite keeps them as plain
strings, so there it is the column alone.

`downgrade` drops the column. PostgreSQL cannot remove a value from an enum type, so on that dialect
the added values stay behind, unused.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-27

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_NEW_ENUM_VALUES = (
    ("txn_type", "FROZEN_PARK"),
    ("txn_type", "FROZEN_RELEASE"),
    ("txn_account", "FROZEN"),
    ("refund_state", "FROZEN"),
    ("order_event_kind", "REFUND_FROZEN"),
    ("order_event_kind", "REFUND_UNFROZEN"),
)


def upgrade() -> None:
    with op.batch_alter_table("wallets") as batch:
        batch.add_column(
            sa.Column("frozen_balance_minor", sa.Integer(), nullable=False, server_default="0")
        )
    if op.get_bind().dialect.name == "postgresql":
        for type_name, value in _NEW_ENUM_VALUES:
            op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    with op.batch_alter_table("wallets") as batch:
        batch.drop_column("frozen_balance_minor")
