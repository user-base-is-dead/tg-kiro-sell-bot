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
    # Columns already created by 0001 migration (which uses Base.metadata.create_all)
    pass


def downgrade() -> None:
    pass
