"""Let a shared account hold many copies of one login, per product.

Migration 0027 made `stock_items.content_hash` unique so a credential could reach exactly one buyer.
That is right for a licence key and wrong for a *shared* account — one login the shop deliberately
sells to many people — where every copy is the same text and the fingerprint rejected all but the
first: pasting the credentials came back "Skipped 1 duplicate(s)" with nothing added.

`products.allow_duplicate_stock` is the opt-out, off everywhere by default so nothing changes for an
existing product. With it on, stock added to that product is stored with `content_hash = NULL`
(see `catalog_service.add_stock`), and NULL is exempt from a unique index on both SQLite and
Postgres — so copies pile up freely there while the index keeps enforcing one-login-one-sale for
every product that did not opt out. The unique index itself is deliberately left alone: dropping it
would have handed the same relaxation to every single-use key in the catalog.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-01

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so the column lands NOT NULL on a table that already has rows; every existing
    # product keeps the strict behaviour it had before this revision.
    with op.batch_alter_table("products") as batch:
        batch.add_column(
            sa.Column(
                "allow_duplicate_stock",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    # Stock added while the flag was on keeps its NULL content_hash: those rows are real sales-in-
    # waiting, and back-filling fingerprints for them would collide on the unique index by design.
    with op.batch_alter_table("products") as batch:
        batch.drop_column("allow_duplicate_stock")
