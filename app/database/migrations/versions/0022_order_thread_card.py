"""Remember the card at the top of an order's topic, so it can be kept true.

The card was posted once, at the instant the order was placed — before it was fulfilled, declined or
refunded — and never touched again. Staff scrolling the orders group saw a pending order forever and
had to read the event lines underneath to work out what it had actually become.

`orders.thread_card_message_id` is that message. With it, every sync edits the card back into the
current state of the order instead of leaving a snapshot of its first second.

Existing orders get NULL: their card's id was never captured, so they keep the frozen one and their
event lines carry the story.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-20

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("thread_card_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("thread_card_message_id")
