"""Remove the Invite & Earn feature: drop the referrals table and the two referral columns on users.

`users.referral_code` (every user's personal invite code, unique) and `users.referred_by_id` (who
invited them, a self-reference) existed only for referrals, as did the `referrals` table recording
who brought in whom and what reward it paid. The feature has been taken out of the bot entirely.

Nothing else points at `referrals`. On SQLite, dropping columns rebuilds `users` (batch mode): the
unique index on the code goes first, and the self-referencing foreign key goes with its column.
Other tables' foreign keys into `users.id` are untouched — the rebuilt table keeps its name and ids.

`downgrade` brings the schema back, empty: `referral_code` returns nullable, because the codes that
were handed out are gone and a NOT NULL column could not be filled.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-27

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("referrals")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_referral_code")
        batch.drop_column("referral_code")
        batch.drop_column("referred_by_id")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("referred_by_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("referral_code", sa.String(length=16), nullable=True))
        batch.create_foreign_key("fk_users_referred_by_id", "users", ["referred_by_id"], ["id"])
        batch.create_index("ix_users_referral_code", ["referral_code"], unique=True)

    op.create_table(
        "referrals",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("referrer_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("referee_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reward_minor", sa.Integer(), nullable=False),
        sa.Column("reward_txn_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_referrals_referrer_id", "referrals", ["referrer_id"])
