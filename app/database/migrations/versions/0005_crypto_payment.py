"""Add CryptoPayment table for cryptocurrency payments.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-09 07:15:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crypto_payments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("product_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("expected_amount", sa.String(64), nullable=False),
        sa.Column("actual_amount", sa.String(64), nullable=True),
        sa.Column("fee_amount", sa.String(64), nullable=True),
        sa.Column("currency", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("description", sa.String(512), nullable=True),
        sa.Column("tx_hash", sa.String(256), nullable=True),
        sa.Column("wallet_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_crypto_payments_user_id", "crypto_payments", ["user_id"])
    op.create_index("ix_crypto_payments_tx_hash", "crypto_payments", ["tx_hash"])


def downgrade() -> None:
    op.drop_index("ix_crypto_payments_tx_hash", table_name="crypto_payments")
    op.drop_index("ix_crypto_payments_user_id", table_name="crypto_payments")
    op.drop_table("crypto_payments")
