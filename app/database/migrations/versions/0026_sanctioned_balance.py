"""Add the Sanctioned balance: Refund Wallet money that staff have blocked.

After a decline has parked an order's money in the Refund Wallet, staff can now sanction any part of
it. The sanctioned amount moves to its own balance: the buyer sees it on their wallet, but it can be
neither paid out nor spent until staff release it back into the Refund Wallet.

`wallets.sanctioned_balance_minor` is that balance; every existing wallet starts at 0. The enums gain
the values the new flow writes:

  txn_type          SANCTION, SANCTION_RELEASE
  txn_account       SANCTIONED
  order_event_kind  REFUND_SANCTIONED, SANCTION_RELEASED

Only PostgreSQL stores those as real enum types that need extending. SQLite keeps them as plain
strings, so there it is the column alone.

`downgrade` drops the column. PostgreSQL cannot remove a value from an enum type, so on that dialect
the added values stay behind, unused.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-27

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_NEW_ENUM_VALUES = (
    ("txn_type", "SANCTION"),
    ("txn_type", "SANCTION_RELEASE"),
    ("txn_account", "SANCTIONED"),
    ("order_event_kind", "REFUND_SANCTIONED"),
    ("order_event_kind", "SANCTION_RELEASED"),
)


def upgrade() -> None:
    with op.batch_alter_table("wallets") as batch:
        batch.add_column(
            sa.Column("sanctioned_balance_minor", sa.Integer(), nullable=False, server_default="0")
        )
    if op.get_bind().dialect.name == "postgresql":
        for type_name, value in _NEW_ENUM_VALUES:
            op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    with op.batch_alter_table("wallets") as batch:
        batch.drop_column("sanctioned_balance_minor")
