"""Let a product exist outside every category.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-10

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column("category_id", existing_type=sa.BigInteger(), nullable=True)

    # The old admin flow fabricated a real "Uncategorized" Category the first time anyone picked
    # it, and it then showed up as a folder in the buyer-facing store. Empty its products out to
    # NULL and delete it. A no-op on any install where nobody ever picked that option.
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT id FROM categories WHERE slug = 'uncategorized'")
    ).fetchone()
    if row is not None:
        conn.execute(
            sa.text("UPDATE products SET category_id = NULL WHERE category_id = :cid"),
            {"cid": row[0]},
        )
        conn.execute(sa.text("DELETE FROM categories WHERE id = :cid"), {"cid": row[0]})


# Minimal column set for the downgrade INSERT. created_at/updated_at carry server defaults, but
# sort_order and is_active are Python-side defaults on the model, so a raw INSERT has to name them
# or they land NULL against a NOT NULL column. Declared through sa.table so the boolean renders
# per-dialect instead of hardcoding `true` vs `1`.
_categories = sa.table(
    "categories",
    sa.column("name", sa.String),
    sa.column("slug", sa.String),
    sa.column("sort_order", sa.Integer),
    sa.column("is_active", sa.Boolean),
)


def downgrade() -> None:
    # Recreate the folder and refile everything loose into it, because the column cannot go back
    # to NOT NULL while any row holds a NULL.
    conn = op.get_bind()
    loose = conn.execute(
        sa.text("SELECT COUNT(*) FROM products WHERE category_id IS NULL")
    ).scalar_one()

    if loose:
        conn.execute(
            _categories.insert().values(
                name="Uncategorized", slug="uncategorized", sort_order=9999, is_active=True
            )
        )
        row = conn.execute(
            sa.text("SELECT id FROM categories WHERE slug = 'uncategorized'")
        ).fetchone()
        conn.execute(
            sa.text("UPDATE products SET category_id = :cid WHERE category_id IS NULL"),
            {"cid": row[0]},
        )

    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column("category_id", existing_type=sa.BigInteger(), nullable=False)
