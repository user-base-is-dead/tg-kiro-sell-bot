"""Remember which reply panel each user already has installed.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-09

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("users")]
    if "panel_signature" not in columns:
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(sa.Column("panel_signature", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("panel_signature")
