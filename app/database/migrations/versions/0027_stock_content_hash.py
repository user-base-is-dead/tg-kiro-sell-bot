"""One login, one sale: fingerprint every stock item and make the fingerprint unique.

The same credential uploaded twice used to become two stock items — and so two sales of one login.
Ciphertext cannot catch that (Fernet encrypts the same text differently every time), so each row now
carries `content_hash`: a keyed fingerprint of its normalised text (`app.core.security.
stock_fingerprint`). A unique index on it makes a second copy impossible to store, in any product,
sold rows included.

Existing rows are fingerprinted here, which means decrypting them — this revision needs the same
ENCRYPTION_KEY as the bot. Where the same login already exists more than once, one row keeps the
fingerprint (a sold copy first, since that one is somebody's purchase), and every extra copy that is
still unsold (AVAILABLE, or HELD) is set VOID: off the shelf, never to be sold. Extra copies that were
already sold cannot be taken back; they keep a NULL fingerprint and the count is logged. VOID rows
are left without one. A row that cannot be decrypted is left NULL too, and counted.

`downgrade` drops the index and the column. Rows set VOID here stay VOID.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-27

"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

# Which copy of a duplicated login keeps its fingerprint: the one somebody has, if anybody has one.
_KEEP_FIRST = {"DELIVERED": 0, "RESERVED": 1, "HELD": 2, "AVAILABLE": 3}


def _fingerprint_existing_rows() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, payload, status FROM stock_items WHERE status <> 'VOID' ORDER BY id")
    ).fetchall()
    if not rows:
        return

    # Imported here, not at module level: only a database that actually has stock needs the key.
    from app.core.security import get_cipher, stock_fingerprint

    cipher = get_cipher()
    groups: dict[str, list[tuple[int, int, str]]] = {}
    unreadable = 0
    for row_id, payload, status in rows:
        try:
            fingerprint = stock_fingerprint(cipher.decrypt(payload))
        except Exception:  # noqa: BLE001 - a row we cannot read must not stop the upgrade
            unreadable += 1
            continue
        groups.setdefault(fingerprint, []).append((_KEEP_FIRST.get(status, 9), row_id, status))

    voided = sold_twice = 0
    for fingerprint, copies in groups.items():
        copies.sort()
        bind.execute(
            sa.text("UPDATE stock_items SET content_hash = :fp WHERE id = :id"),
            {"fp": fingerprint, "id": copies[0][1]},
        )
        for _rank, row_id, status in copies[1:]:
            if status in ("AVAILABLE", "HELD"):
                bind.execute(
                    sa.text(
                        "UPDATE stock_items SET status = 'VOID', held_by_user_id = NULL, "
                        "held_at = NULL, held_until = NULL WHERE id = :id"
                    ),
                    {"id": row_id},
                )
                voided += 1
            else:
                sold_twice += 1

    log.info(
        "stock fingerprints: %d row(s) fingerprinted, %d unsold duplicate(s) set VOID, "
        "%d duplicate(s) already sold, %d unreadable",
        len(groups),
        voided,
        sold_twice,
        unreadable,
    )


def upgrade() -> None:
    with op.batch_alter_table("stock_items") as batch:
        batch.add_column(sa.Column("content_hash", sa.String(length=64), nullable=True))
    _fingerprint_existing_rows()
    op.create_index("ux_stock_items_content_hash", "stock_items", ["content_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_stock_items_content_hash", table_name="stock_items")
    with op.batch_alter_table("stock_items") as batch:
        batch.drop_column("content_hash")
