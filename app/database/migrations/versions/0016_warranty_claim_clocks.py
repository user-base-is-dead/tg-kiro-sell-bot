"""Split the warranty clock from the claim-processing clock.

A warranty claim used to carry a single timestamp, `claim_started_at`, and the 24h staff deadline
was recomputed from it inside a query. That conflated two independent timers: how long the product's
warranty runs, and how long staff have to answer a claim about it. It also left `/done` with nothing
to pay out from — it was reduced to guessing the remaining warranty by subtracting review time from
the full duration, which is not the same number.

This adds the missing state so warranty status is recoverable from the database alone, with no
in-memory timer and no dependence on the bot having been online:

  claim_deadline_at       — absolute deadline for staff to answer; drives auto-rejection
  claim_remaining_seconds — warranty time left at the moment the claim was filed, frozen, and the
                            amount `/done` grants to the replacement item
  claim_resolved_at       — when /done succeeded
  claim_rejected_at       — when /reject or auto-rejection landed

`warranties.expires_at` is untouched and remains the source of truth for the warranty itself. It is
never rewritten by filing a claim, so a warranty that lapses mid-review is simply expired.

Backfill: existing CLAIMED rows get a deadline of claim_started_at + 24h (the rule that was
previously hardcoded) and a frozen remainder of expires_at - claim_started_at, which reproduces what
those claims were already entitled to. Rows in any other state keep NULLs.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-10

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

CLAIM_GRACE_HOURS = 24


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    warr_cols = [c["name"] for c in inspector.get_columns("warranties")]
    if "claim_deadline_at" not in warr_cols:
        with op.batch_alter_table("warranties") as batch:
            batch.add_column(sa.Column("claim_deadline_at", sa.DateTime(timezone=True), nullable=True))
            batch.add_column(sa.Column("claim_remaining_seconds", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("claim_resolved_at", sa.DateTime(timezone=True), nullable=True))
            batch.add_column(sa.Column("claim_rejected_at", sa.DateTime(timezone=True), nullable=True))

        op.create_index("ix_warranties_claim_deadline_at", "warranties", ["claim_deadline_at"])

    # Dialect-specific because there is no portable "add N hours to a timestamp" or "difference in
    # seconds" expression, and both are needed to reproduce the old implicit rule exactly.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        deadline = f"datetime(claim_started_at, '+{CLAIM_GRACE_HOURS} hours')"
        # SQLite's two-argument max() is scalar, unlike the aggregate of the same name.
        remaining = "MAX(0, CAST(strftime('%s', expires_at) - strftime('%s', claim_started_at) AS INTEGER))"
    else:
        deadline = f"claim_started_at + interval '{CLAIM_GRACE_HOURS} hours'"
        remaining = "GREATEST(0, CAST(EXTRACT(EPOCH FROM (expires_at - claim_started_at)) AS INTEGER))"

    op.execute(
        sa.text(
            f"UPDATE warranties SET claim_deadline_at = {deadline}, "  # noqa: S608 - dialect literals, no user input
            f"claim_remaining_seconds = {remaining} "
            "WHERE status = 'CLAIMED' AND claim_started_at IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_warranties_claim_deadline_at", table_name="warranties")
    with op.batch_alter_table("warranties") as batch:
        batch.drop_column("claim_rejected_at")
        batch.drop_column("claim_resolved_at")
        batch.drop_column("claim_remaining_seconds")
        batch.drop_column("claim_deadline_at")
