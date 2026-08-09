"""Add CryptoPayment table for cryptocurrency payments.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-09 07:15:00.000000

"""
from __future__ import annotations


# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Table already created by 0001 migration (which uses Base.metadata.create_all)
    pass


def downgrade() -> None:
    pass
