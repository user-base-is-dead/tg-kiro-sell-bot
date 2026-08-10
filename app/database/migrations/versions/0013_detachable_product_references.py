"""Let a product be deleted outright without destroying order history.

`order_items.product_id` and `stock_items.product_id` become nullable so `ProductRepo.delete` can
detach them instead of being blocked by the foreign key. Both rows already snapshot everything they
need to render on their own — `order_items` carries product_name/unit_price_minor/warranty_days,
`stock_items` carries the delivered payload — so a NULL costs the link back to the catalog and
nothing the buyer can see.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-10

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("order_items") as batch_op:
        batch_op.alter_column("product_id", existing_type=sa.BigInteger(), nullable=True)
    with op.batch_alter_table("stock_items") as batch_op:
        batch_op.alter_column("product_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    # The column cannot go back to NOT NULL while any row holds a NULL, and there is no product to
    # point a detached row back at — it was deleted. Dropping those rows is the only way back, and
    # it is exactly the history loss this migration exists to prevent, so the downgrade is refused
    # rather than made quietly destructive.
    conn = op.get_bind()
    detached = conn.execute(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM order_items WHERE product_id IS NULL)"
            " + (SELECT COUNT(*) FROM stock_items WHERE product_id IS NULL)"
        )
    ).scalar_one()
    if detached:
        raise RuntimeError(
            f"{detached} row(s) reference a deleted product. Downgrading would have to delete that "
            "order/delivery history. Remove or re-point those rows by hand first."
        )

    with op.batch_alter_table("stock_items") as batch_op:
        batch_op.alter_column("product_id", existing_type=sa.BigInteger(), nullable=False)
    with op.batch_alter_table("order_items") as batch_op:
        batch_op.alter_column("product_id", existing_type=sa.BigInteger(), nullable=False)
