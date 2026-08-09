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
    op.add_column("warranties", sa.Column("claim_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("warranties", sa.Column("claim_ticket_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_warranties_claim_ticket_id", "warranties", ["claim_ticket_id"])
    op.create_foreign_key(
        "fk_warranties_claim_ticket_id", "warranties", "support_tickets", ["claim_ticket_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_warranties_claim_ticket_id", "warranties", type_="foreignkey")
    op.drop_index("ix_warranties_claim_ticket_id", table_name="warranties")
    op.drop_column("warranties", "claim_ticket_id")
    op.drop_column("warranties", "claim_started_at")
