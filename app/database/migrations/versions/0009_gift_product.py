"""Let a gift code grant a product instead of wallet credit.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-10

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("gift_codes") as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.Enum("CREDIT", "PRODUCT", name="gift_kind"),
                nullable=False,
                server_default="CREDIT",
            )
        )
        batch_op.add_column(sa.Column("product_id", sa.BigInteger(), nullable=True))
        # Every code that exists today is a credit code, so the backfill is the server_default
        # above. value_minor only becomes optional now that PRODUCT codes leave it empty.
        batch_op.alter_column("value_minor", existing_type=sa.Integer(), nullable=True)
        batch_op.create_foreign_key(
            "fk_gift_codes_product_id", "products", ["product_id"], ["id"]
        )

    with op.batch_alter_table("gift_redemptions") as batch_op:
        # A product claim moves no money, so it records the order it created instead. Both columns
        # become optional because exactly one applies per redemption.
        batch_op.add_column(sa.Column("order_id", sa.Uuid(as_uuid=False), nullable=True))
        batch_op.alter_column("wallet_transaction_id", existing_type=sa.BigInteger(), nullable=True)
        batch_op.create_foreign_key(
            "fk_gift_redemptions_order_id", "orders", ["order_id"], ["id"]
        )


def downgrade() -> None:
    # Product gifts have no representation in the old schema; dropping the column would leave them
    # as codes granting nothing. Fail loudly instead of silently corrupting them.
    bind = op.get_bind()
    stranded = bind.execute(
        sa.text("SELECT COUNT(*) FROM gift_codes WHERE kind = 'PRODUCT'")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"{stranded} PRODUCT gift code(s) exist and cannot be represented before 0009. "
            "Disable or delete them before downgrading."
        )

    with op.batch_alter_table("gift_redemptions") as batch_op:
        batch_op.drop_constraint("fk_gift_redemptions_order_id", type_="foreignkey")
        batch_op.alter_column("wallet_transaction_id", existing_type=sa.BigInteger(), nullable=False)
        batch_op.drop_column("order_id")

    with op.batch_alter_table("gift_codes") as batch_op:
        batch_op.drop_constraint("fk_gift_codes_product_id", type_="foreignkey")
        batch_op.alter_column("value_minor", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("product_id")
        batch_op.drop_column("kind")
