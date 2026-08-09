"""Add OrderHold table for 5-minute payment window.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-09 07:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Table already created by 0001 migration (which uses Base.metadata.create_all)
    pass


def downgrade() -> None:
    pass
