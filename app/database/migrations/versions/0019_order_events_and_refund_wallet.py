"""Give every order a searchable history, and refunds a wallet of their own.

Two problems, one shape.

An order used to remember only its current state. Cancelling it wrote "Cancelled by admin" into
`failure_reason` and credited the wallet, so a month later nobody could say why a particular order
died, who killed it, or whether the money had gone anywhere — the audit log had one line naming the
admin and the order id, and that was the whole record. `order_events` fixes that: one immutable row
per thing that ever happens to an order, each carrying its own prefixed ID (`DEC-…` for a decline,
`RFD-…` for a refund) that an admin can paste into a search box and land back on the order.

The money is the second half. A cancelled order used to credit the buyer's spendable balance, which
quietly decided something the store had not: that somebody who paid in USDT wants shop credit. That
cannot be reversed once they spend it. `wallets.refund_balance_minor` is a second balance that
`wallet_service.debit` cannot reach, so a refund lands somewhere the buyer can see and nobody can
spend, and an admin chooses what happens next — send it on chain and record the payout, or move it
across into spendable balance. `wallet_transactions.account` splits the ledger the same way, so each
balance stays checkable against its own rows.

Backfill: every existing order gets a synthetic PLACED event, delivered ones a DELIVERED, and
cancelled ones a DECLINED carrying whatever `failure_reason` held — so search works on history from
day one rather than starting empty. Already-cancelled orders are marked SETTLED, because the old code
really did put that money in the spendable balance and this migration must not imply it is still
owed. **No money is moved by this migration.**

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-19

"""
from __future__ import annotations

import secrets

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


# Mirrors app.core.security.EVENT_PREFIXES. Duplicated rather than imported: a migration has to keep
# doing what it did on the day it was written, even after the application code moves on.
_PREFIXES = {
    "PLACED": "PLC",
    "DELIVERED": "DLV",
    "DECLINED": "DEC",
    "REFUND_PARKED": "RFD",
    "REFUND_PAID_OUT": "PAY",
    "REFUND_MOVED": "MOV",
    "TICKET_OPENED": "TKT",
}


def _event_number(kind: str, taken: set[str]) -> str:
    """A unique ID for a backfilled event. `taken` guards within this one batch; the unique index
    guards everything else."""
    prefix = _PREFIXES.get(kind, "EVT")
    while True:
        candidate = f"{prefix}-{secrets.token_hex(3).upper()}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate


def upgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    # Postgres needs the enum types to exist before a column can be typed with them, and the two new
    # labels on `txn_type` before a row can carry them. SQLite stores every one of these as VARCHAR
    # and needs none of it.
    if is_pg:
        op.execute(
            "CREATE TYPE order_event_kind AS ENUM ('PLACED', 'DELIVERED', 'DECLINED', "
            "'REFUND_PARKED', 'REFUND_PAID_OUT', 'REFUND_MOVED', 'TICKET_OPENED')"
        )
        op.execute("CREATE TYPE order_event_actor AS ENUM ('SYSTEM', 'ADMIN', 'USER')")
        op.execute("CREATE TYPE funding_source AS ENUM ('WALLET', 'CRYPTO')")
        op.execute("CREATE TYPE refund_state AS ENUM ('NONE', 'PARKED', 'SETTLED')")
        op.execute("CREATE TYPE txn_account AS ENUM ('MAIN', 'REFUND')")
        with op.get_context().autocommit_block():
            for label in ("REFUND_PARK", "REFUND_PAYOUT", "REFUND_MOVE", "REFUND_ADJUST"):
                op.execute(f"ALTER TYPE txn_type ADD VALUE IF NOT EXISTS '{label}'")

    kind_type = sa.Enum(name="order_event_kind", create_type=False) if is_pg else sa.String(length=32)
    actor_type = sa.Enum(name="order_event_actor", create_type=False) if is_pg else sa.String(length=16)
    funding_type = sa.Enum(name="funding_source", create_type=False) if is_pg else sa.String(length=16)
    refund_type = sa.Enum(name="refund_state", create_type=False) if is_pg else sa.String(length=16)
    account_type = sa.Enum(name="txn_account", create_type=False) if is_pg else sa.String(length=16)

    op.create_table(
        "order_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("event_number", sa.String(length=24), nullable=False),
        sa.Column("order_id", sa.Uuid(as_uuid=False), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("kind", kind_type, nullable=False),
        sa.Column("actor", actor_type, nullable=False, server_default="SYSTEM"),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("amount_minor", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("reference", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_order_events_event_number", "order_events", ["event_number"], unique=True)
    op.create_index("ix_order_events_order_id", "order_events", ["order_id"])
    op.create_index("ix_order_events_created_at", "order_events", ["created_at"])
    op.create_index("ix_order_events_order_created", "order_events", ["order_id", "created_at"])

    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("cancelled_by_admin_id", sa.BigInteger(), nullable=True))
        batch.add_column(
            sa.Column("funding_source", funding_type, nullable=False, server_default="WALLET")
        )
        batch.add_column(sa.Column("crypto_payment_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("refund_state", refund_type, nullable=False, server_default="NONE"))
        batch.add_column(sa.Column("refund_amount_minor", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("refund_ticket_id", sa.BigInteger(), nullable=True))

    with op.batch_alter_table("wallets") as batch:
        batch.add_column(
            sa.Column("refund_balance_minor", sa.Integer(), nullable=False, server_default="0")
        )

    with op.batch_alter_table("wallet_transactions") as batch:
        batch.add_column(sa.Column("account", account_type, nullable=False, server_default="MAIN"))
    op.create_index(
        "ix_wallet_txn_wallet_account", "wallet_transactions", ["wallet_id", "account"]
    )

    with op.batch_alter_table("crypto_payments") as batch:
        batch.add_column(sa.Column("order_id", sa.Uuid(as_uuid=False), nullable=True))
        batch.create_index("ix_crypto_payments_order_id", ["order_id"])

    _backfill(conn)


def _backfill(conn) -> None:
    """Give the orders that already exist enough history to be searchable.

    Reconstructed from the columns rather than invented: `placed_at` is when it was placed,
    `completed_at` is when it was delivered, `cancelled_at` and `failure_reason` are the decline. No
    refund events are written — the old code credited the spendable balance and that money is long
    since spendable, so claiming a parked refund here would invent a debt.
    """
    orders = conn.execute(
        sa.text(
            "SELECT id, status, total_minor, currency, placed_at, completed_at, cancelled_at, "
            "failure_reason, payment_txn_id FROM orders"
        )
    ).fetchall()
    if not orders:
        return

    taken: set[str] = set()
    rows = []
    settled_ids = []

    for row in orders:
        placed_at = row.placed_at or row.completed_at or row.cancelled_at
        if placed_at is not None:
            rows.append(
                {
                    "event_number": _event_number("PLACED", taken),
                    "order_id": row.id,
                    "kind": "PLACED",
                    "actor": "USER",
                    "amount_minor": row.total_minor,
                    "currency": row.currency,
                    "reason": "Recorded before order history existed",
                    "created_at": placed_at,
                }
            )
        if row.completed_at is not None:
            rows.append(
                {
                    "event_number": _event_number("DELIVERED", taken),
                    "order_id": row.id,
                    "kind": "DELIVERED",
                    "actor": "SYSTEM",
                    "amount_minor": None,
                    "currency": None,
                    "reason": "Delivered",
                    "created_at": row.completed_at,
                }
            )
        if row.cancelled_at is not None or row.status in ("CANCELLED", "FAILED"):
            rows.append(
                {
                    "event_number": _event_number("DECLINED", taken),
                    "order_id": row.id,
                    "kind": "DECLINED",
                    "actor": "SYSTEM",
                    "amount_minor": None,
                    "currency": None,
                    "reason": row.failure_reason or "Cancelled before a reason was recorded",
                    "created_at": row.cancelled_at or placed_at,
                }
            )
            # The old cancel path credited the spendable wallet, so this money is not owed. Marked
            # SETTLED so it never shows up in the Refund Wallets queue as an unpaid debt.
            if row.payment_txn_id is not None:
                settled_ids.append(row.id)

    rows = [r for r in rows if r["created_at"] is not None]
    if rows:
        conn.execute(
            sa.text(
                "INSERT INTO order_events (event_number, order_id, kind, actor, amount_minor, "
                "currency, reason, created_at) VALUES (:event_number, :order_id, :kind, :actor, "
                ":amount_minor, :currency, :reason, :created_at)"
            ),
            rows,
        )

    for order_id in settled_ids:
        conn.execute(
            sa.text(
                "UPDATE orders SET refund_state = 'SETTLED', refund_amount_minor = total_minor "
                "WHERE id = :id"
            ),
            {"id": order_id},
        )


def downgrade() -> None:
    with op.batch_alter_table("crypto_payments") as batch:
        batch.drop_index("ix_crypto_payments_order_id")
        batch.drop_column("order_id")

    op.drop_index("ix_wallet_txn_wallet_account", table_name="wallet_transactions")
    with op.batch_alter_table("wallet_transactions") as batch:
        batch.drop_column("account")

    with op.batch_alter_table("wallets") as batch:
        batch.drop_column("refund_balance_minor")

    with op.batch_alter_table("orders") as batch:
        batch.drop_column("refund_ticket_id")
        batch.drop_column("refund_amount_minor")
        batch.drop_column("refund_state")
        batch.drop_column("crypto_payment_id")
        batch.drop_column("funding_source")
        batch.drop_column("cancelled_by_admin_id")

    op.drop_index("ix_order_events_order_created", table_name="order_events")
    op.drop_index("ix_order_events_created_at", table_name="order_events")
    op.drop_index("ix_order_events_order_id", table_name="order_events")
    op.drop_index("ix_order_events_event_number", table_name="order_events")
    op.drop_table("order_events")

    if op.get_bind().dialect.name == "postgresql":
        for type_name in ("txn_account", "refund_state", "funding_source", "order_event_actor", "order_event_kind"):
            op.execute(f"DROP TYPE IF EXISTS {type_name}")
        # `txn_type`'s four added labels are deliberately left in place: Postgres cannot drop an enum
        # value, and any REFUND_* ledger row written while 0019 was live still needs them to be
        # readable. They are inert once the refund balance column is gone.
