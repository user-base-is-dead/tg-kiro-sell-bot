"""Let a ticket live in the order's own forum topic, so a refund is argued where the order is logged.

A declined order used to open a ticket in SUPPORT_GROUP_ID, next to the order's separate log topic in
ORDERS_GROUP_ID. Two threads about one refund: staff read the money in one and answered the buyer in
the other, and the buyer's side of it was a ticket that said nothing about the order.

`support_tickets.group_chat_id` is which chat a ticket's `topic_id` belongs to. NULL means
SUPPORT_GROUP_ID — every existing ticket, and every ordinary ticket opened from now on. An order
dispute sets it to ORDERS_GROUP_ID and reuses the order's own topic, so the refund is settled in the
thread that already carries the order's history.

It is stored rather than looked up from the env on each read, because the env can be repointed at a
new group while a ticket is still live, and that ticket has to keep talking to the group it was
actually opened in.

`support_tickets.order_id` marks that a ticket IS such a dispute (and which order). That is what the
one-conversation rule keys off: while it is open the buyer's messages go into the order thread and
Create Ticket / Warranty stay shut, until an admin runs /close.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-19

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("support_tickets") as batch:
        batch.add_column(sa.Column("group_chat_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("order_id", sa.String(length=36), nullable=True))
    op.create_index("ix_support_tickets_order_id", "support_tickets", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_support_tickets_order_id", table_name="support_tickets")
    with op.batch_alter_table("support_tickets") as batch:
        batch.drop_column("order_id")
        batch.drop_column("group_chat_id")
