"""Let a broadcast carry media by storing the admin's original message coordinates.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-10

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("broadcasts") as batch_op:
        batch_op.add_column(sa.Column("parts_json", sa.Text(), nullable=True))


def downgrade() -> None:
    # Nullable and additive: existing rows keep sending `body` as plain text, so dropping it only
    # loses media composition for drafts that never sent.
    with op.batch_alter_table("broadcasts") as batch_op:
        batch_op.drop_column("parts_json")
