"""Add warranty claim tracking fields.

`claim_started_at` / `claim_ticket_id` were already read by the claim handler, the warranty
repo and the auto-reject job, but never existed as columns — this adds them.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("warranties") as batch_op:
        batch_op.add_column(sa.Column("claim_started_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("claim_ticket_id", sa.BigInteger(), nullable=True))
        batch_op.create_index("ix_warranties_claim_ticket_id", ["claim_ticket_id"])


def downgrade() -> None:
    with op.batch_alter_table("warranties") as batch_op:
        batch_op.drop_index("ix_warranties_claim_ticket_id")
        batch_op.drop_column("claim_ticket_id")
        batch_op.drop_column("claim_started_at")
