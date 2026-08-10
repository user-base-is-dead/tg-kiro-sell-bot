"""Move reservations from the product to the individual credential.

`order_holds` reserved a whole product for one buyer for five minutes. That is the wrong grain: a
product is backed by many independent credentials, so reserving the product made every one of them
look unavailable while only one was being bought, and 19 free logins sat idle behind one checkout.

The reservation now lives on the `stock_items` row it actually applies to — `status = 'HELD'` plus
`held_by_user_id` / `held_at` / `held_until`. One row, one source of truth: a credential cannot be
HELD in one table and AVAILABLE in another, which is the only way the same login reaches two people.

`order_holds` is dropped rather than left behind. Its rows are five-minute reservations, so there is
no history to preserve, and a dead table describing the old model is an invitation to rebuild it.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-10

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stock_items") as batch_op:
        batch_op.add_column(sa.Column("held_by_user_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("held_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("held_until", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            "fk_stock_items_held_by_user_id", "users", ["held_by_user_id"], ["id"]
        )
        batch_op.create_index("ix_stock_items_held_by_user_id", ["held_by_user_id"])
        # The expiry sweep is `WHERE status = 'HELD' AND held_until <= now`, on every tick.
        batch_op.create_index("ix_stock_items_status_held_until", ["status", "held_until"])

    # Postgres stores StockStatus as a native enum, so the new label has to be declared before any
    # row can carry it. SQLite keeps it as VARCHAR and needs nothing. ALTER TYPE cannot run inside a
    # transaction block on older servers, hence the autocommit block.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE stock_status ADD VALUE IF NOT EXISTS 'HELD'")

    op.drop_table("order_holds")


def downgrade() -> None:
    # Any live hold has to go back to the shelf: the old model has nowhere to record it, and leaving
    # rows stuck on a status the code no longer knows would make those credentials unsellable.
    op.execute(
        "UPDATE stock_items SET status = 'AVAILABLE', held_by_user_id = NULL, "
        "held_at = NULL, held_until = NULL WHERE status = 'HELD'"
    )

    op.create_table(
        "order_holds",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=True),
    )
    op.create_index("ix_order_holds_product_id", "order_holds", ["product_id"])
    op.create_index("ix_order_holds_user_id", "order_holds", ["user_id"])
    op.create_index("ix_order_holds_order_id", "order_holds", ["order_id"])
    op.create_index("ix_order_holds_product_expires", "order_holds", ["product_id", "expires_at"])

    with op.batch_alter_table("stock_items") as batch_op:
        batch_op.drop_index("ix_stock_items_status_held_until")
        batch_op.drop_index("ix_stock_items_held_by_user_id")
        batch_op.drop_constraint("fk_stock_items_held_by_user_id", type_="foreignkey")
        batch_op.drop_column("held_until")
        batch_op.drop_column("held_at")
        batch_op.drop_column("held_by_user_id")
