"""Give gift codes their own stock instead of borrowing the catalog's.

A gift code used to be able to point at a catalog product (`gift_codes.product_id`, `kind=PRODUCT`)
and hand it over as a free order, consuming a real `stock_items` row. A giveaway is not a sale: that
let a promo quietly empty the shelf paying customers were queueing for, and it meant gift stock
could not be counted, topped up, or reasoned about on its own.

`gift_items` replaces it. Each row is one giveaway credential owned by one code, encrypted like any
other payload, claimed by at most one user. `kind` becomes ITEM.

`gift_codes.product_id` is dropped. This is safe on any existing install: PRODUCT codes were only
reachable through the admin wizard, and the column carries no history — the redemption record lives
in `gift_redemptions`, which is untouched. The upgrade refuses to run if any PRODUCT code exists
rather than silently turning it into a code that grants nothing.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-10

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    stranded = conn.execute(
        sa.text("SELECT COUNT(*) FROM gift_codes WHERE kind = 'PRODUCT'")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"{stranded} gift code(s) still grant a catalog product. Disable them in the admin "
            "panel first — gift codes now carry their own items and cannot point at a product."
        )

    op.create_table(
        "gift_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("gift_code_id", sa.BigInteger(), sa.ForeignKey("gift_codes.id"), nullable=False),
        sa.Column("payload", sa.String(length=4096), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="AVAILABLE"),
        sa.Column("claimed_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_gift_items_gift_code_id", "gift_items", ["gift_code_id"])
    op.create_index("ix_gift_items_claimed_by_user_id", "gift_items", ["claimed_by_user_id"])
    op.create_index("ix_gift_items_code_status", "gift_items", ["gift_code_id", "status"])

    # Postgres stores these as native enums, so the new labels must exist before a row can carry
    # them. SQLite keeps both as VARCHAR and needs nothing.
    if conn.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE gift_kind ADD VALUE IF NOT EXISTS 'ITEM'")
        op.execute("CREATE TYPE gift_item_status AS ENUM ('AVAILABLE', 'DELIVERED')")

    with op.batch_alter_table("gift_codes") as batch_op:
        batch_op.drop_column("product_id")


def downgrade() -> None:
    with op.batch_alter_table("gift_codes") as batch_op:
        batch_op.add_column(sa.Column("product_id", sa.BigInteger(), nullable=True))
        batch_op.create_foreign_key("fk_gift_codes_product_id", "products", ["product_id"], ["id"])

    # An ITEM code has no product to point at, so it cannot be expressed in the old model. Disabling
    # is the honest outcome: the code stops working rather than silently granting nothing.
    op.execute("UPDATE gift_codes SET status = 'DISABLED' WHERE kind = 'ITEM'")

    op.drop_index("ix_gift_items_code_status", table_name="gift_items")
    op.drop_index("ix_gift_items_claimed_by_user_id", table_name="gift_items")
    op.drop_index("ix_gift_items_gift_code_id", table_name="gift_items")
    op.drop_table("gift_items")

    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS gift_item_status")
