"""Give each order a forum topic to log itself into.

Every support ticket already gets its own topic in SUPPORT_GROUP_ID, which is what makes a
conversation followable. An order had nothing equivalent: the only way to see what had happened to
one was to open the admin panel and look, and nothing at all reached staff when an order moved on
its own — an auto-delivery, a refund settled by somebody else.

`orders.thread_id` is that topic, in a SEPARATE group (ORDERS_GROUP_ID). Separate on purpose: support
topics are threads a buyer is waiting for a reply in, and interleaving a running order log with them
would bury the messages staff have to answer.

`orders.thread_last_event_id` is how far the log has been caught up. Posting works off the
already-recorded `order_events` rows rather than off the action being performed, so bringing a thread
up to date is "post everything newer than this id" — idempotent, and a post that failed once is
retried by the next action instead of being lost.

Existing orders get NULL for both. They have no topic and never will; their history is intact in
`order_events` and readable from the admin dossier, and back-filling months of finished orders into a
fresh group would bury the live ones. Threads start with the next order placed.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-19

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("thread_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("thread_last_event_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("thread_last_event_id")
        batch.drop_column("thread_id")
