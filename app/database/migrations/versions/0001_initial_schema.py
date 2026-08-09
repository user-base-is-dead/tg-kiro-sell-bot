"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-08

No live Postgres was available while scaffolding this project, so this initial migration
is generated from the SQLAlchemy models' metadata directly (`Base.metadata.create_all`)
rather than hand-written `op.create_table` calls or `alembic revision --autogenerate`
against a running DB. It is a full, correct snapshot of every model in `app/database/models/`.

From the *next* migration onward, run `alembic revision --autogenerate` against a real
database as usual — autogenerate diffs against this baseline normally.
"""
from typing import Sequence, Union

from alembic import op

from app.database import models  # noqa: F401 — registers all tables on Base.metadata
from app.database.base import Base

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
