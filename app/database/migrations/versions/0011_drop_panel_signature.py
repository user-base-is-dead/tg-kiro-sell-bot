"""Drop users.panel_signature — the panel is always re-sent now.

The column existed to skip re-sending an identical reply keyboard. Combined with deleting the
carrier message, that lost the panel outright: the delete removed the keyboard and the cached
signature then stopped anything from restoring it. Re-sending is cheap and self-healing, so both
the delete and the cache are gone and the column has no remaining purpose.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-10

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("panel_signature")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("panel_signature", sa.String(32), nullable=True))
