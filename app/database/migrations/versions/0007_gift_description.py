"""Add description field to gift codes.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-09

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("gift_codes") as batch_op:
        batch_op.add_column(sa.Column("description", sa.String(512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("gift_codes") as batch_op:
        batch_op.drop_column("description")
