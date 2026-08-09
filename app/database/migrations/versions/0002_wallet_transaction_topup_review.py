"""wallet_transactions: proof / admin_note / reviewed_by_admin_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-09

Supports the manual top-up review flow (Phase 4): the user's submitted proof, the admin's
approve/reject note, and who reviewed it.
"""
from typing import Sequence, Union


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Columns already created by 0001 migration (which uses Base.metadata.create_all)
    pass


def downgrade() -> None:
    pass
