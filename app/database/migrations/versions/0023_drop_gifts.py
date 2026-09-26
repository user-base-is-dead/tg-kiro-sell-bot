"""Remove the gifts feature: drop its three tables.

Gift codes, their redemptions and their giveaway items were a whole feature of their own — buyer
screens, an admin wizard, a service — and it has been taken out of the bot entirely. Nothing else
points into these tables: `gift_redemptions` and `gift_items` only point *out* (at users, orders and
wallet transactions), so they can go without touching any other table. Children first, then
`gift_codes`, which both of them reference.

On PostgreSQL the three enum types the columns used are dropped too; SQLite stores those columns as
plain strings and has no types to drop.

`downgrade` rebuilds the tables as they stood at 0022, empty. The rows themselves are not coming
back — a downgrade restores the schema, not the data.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-27

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_ENUM_TYPES = ("gift_item_status", "gift_status", "gift_kind")


def upgrade() -> None:
    op.drop_table("gift_items")
    op.drop_table("gift_redemptions")
    op.drop_table("gift_codes")
    if op.get_bind().dialect.name == "postgresql":
        for name in _ENUM_TYPES:
            op.execute(f"DROP TYPE IF EXISTS {name}")


def downgrade() -> None:
    op.create_table(
        "gift_codes",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("code_last4", sa.String(length=4), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("CREDIT", "ITEM", name="gift_kind"),
            nullable=False,
            server_default="CREDIT",
        ),
        sa.Column("value_minor", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "DISABLED", "EXPIRED", "EXHAUSTED", name="gift_status"),
            nullable=False,
        ),
        sa.Column("created_by_admin_id", sa.BigInteger(), nullable=False),
        sa.Column("per_user_limit", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_gift_codes_code_hash", "gift_codes", ["code_hash"], unique=True)

    op.create_table(
        "gift_redemptions",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("gift_code_id", sa.BigInteger(), sa.ForeignKey("gift_codes.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("wallet_transaction_id", sa.BigInteger(), sa.ForeignKey("wallet_transactions.id"), nullable=True),
        sa.Column("order_id", sa.Uuid(as_uuid=False), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gift_redemptions_gift_code_id", "gift_redemptions", ["gift_code_id"])
    op.create_index("ix_gift_redemptions_user_id", "gift_redemptions", ["user_id"])
    op.create_index(
        "ux_gift_redemptions_code_user", "gift_redemptions", ["gift_code_id", "user_id"], unique=True
    )

    op.create_table(
        "gift_items",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("gift_code_id", sa.BigInteger(), sa.ForeignKey("gift_codes.id"), nullable=False),
        sa.Column("payload", sa.String(length=4096), nullable=False),
        sa.Column(
            "status",
            sa.Enum("AVAILABLE", "DELIVERED", name="gift_item_status"),
            nullable=False,
            server_default="AVAILABLE",
        ),
        sa.Column("claimed_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_gift_items_gift_code_id", "gift_items", ["gift_code_id"])
    op.create_index("ix_gift_items_claimed_by_user_id", "gift_items", ["claimed_by_user_id"])
    op.create_index("ix_gift_items_code_status", "gift_items", ["gift_code_id", "status"])
