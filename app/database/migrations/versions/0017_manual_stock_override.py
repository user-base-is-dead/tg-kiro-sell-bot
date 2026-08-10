"""Let an admin set the sellable stock count by hand, independently of the credential pool.

Until now "how many are left" had exactly one possible answer: count the AVAILABLE stock_items. That
is right for a product whose every unit is a pre-loaded credential, and wrong for everything else —
a product the admin can supply on demand, a batch of keys not yet pasted in, a deliberate cap of 5
sales on a pool of 50.

`products.manual_stock` is that override. NULL means "no override, count the credentials" — which is
every existing row, so this migration needs no backfill and changes no behaviour on its own. When
set, it is the number shown to shoppers and decremented on each sale.

Crucially it does NOT decide how an order is fulfilled. Delivery stays per-order: a buyer gets an
auto-delivered credential whenever one is free, and only the units beyond the credential pool fall
through to manual fulfilment. So an override of 15 against 10 credentials sells 10 automatically and
the last 5 by hand, with no mode switch on the product itself.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-11

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("manual_stock", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("products") as batch:
        batch.drop_column("manual_stock")
