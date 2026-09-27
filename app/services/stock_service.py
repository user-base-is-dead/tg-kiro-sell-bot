"""One stock item at a time — what the admin's 📋 Stock items screen does to a single credential.

Only an unsold credential can be changed. Once a login has been sold it is the record of what that
buyer received, and rewriting or deleting it would leave nobody able to answer "which login did I
get?" when they write in about it.

"Unsold" is decided by the database at the moment of the change, not by what the screen showed a
minute ago: edit and remove are single conditional statements whose `WHERE` restates that the row is
still free (`stock_hold_service.is_free`). If a buyer reserved it in the meantime, `rowcount` is 0
and nothing changes — the same rule every hold and every purchase follows.
"""

from __future__ import annotations

import enum
import re
from datetime import UTC, datetime

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_cipher, normalize_stock_text, stock_fingerprint
from app.database.models.catalog import StockItem
from app.database.repositories.product_repo import ProductRepo
from app.database.repositories.stock_repo import StockRepo
from app.services.catalog_service import MAX_PAYLOAD_BYTES, resync_status
from app.services.stock_hold_service import is_free
from app.utils.text import escape_html


class EditOutcome(enum.Enum):
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    # The new text is a login that is already in stock, or was already sold, somewhere.
    DUPLICATE = "duplicate"
    # Sold, held at checkout, or gone by the time the change reached the database.
    NOT_FREE = "not_free"


async def edit_item(
    session: AsyncSession, *, stock_item_id: int, product_id: int, new_payload: str
) -> tuple[EditOutcome, StockItem | None]:
    """Replace an unsold credential's content. Returns the outcome and, for DUPLICATE, the row that
    already holds that login."""
    payload = new_payload.strip()
    if not normalize_stock_text(payload):
        return EditOutcome.EMPTY, None
    if len(payload.encode()) > MAX_PAYLOAD_BYTES:
        return EditOutcome.TOO_LONG, None

    repo = StockRepo(session)
    item = await repo.get(stock_item_id)
    if item is None or item.product_id != product_id:
        return EditOutcome.NOT_FREE, None

    cipher = get_cipher()
    if cipher.decrypt(item.payload) == payload:
        return EditOutcome.UNCHANGED, None

    fingerprint = stock_fingerprint(payload)
    clash = await repo.find_by_fingerprint(fingerprint)
    if clash is not None and clash.id != item.id:
        return EditOutcome.DUPLICATE, clash

    result = await session.execute(
        update(StockItem)
        .where(
            StockItem.id == stock_item_id,
            StockItem.product_id == product_id,
            is_free(datetime.now(UTC)),
        )
        .values(payload=cipher.encrypt(payload), content_hash=fingerprint)
    )
    await session.flush()
    return (EditOutcome.UPDATED if result.rowcount else EditOutcome.NOT_FREE), None


async def remove_item(session: AsyncSession, *, stock_item_id: int, product_id: int) -> bool:
    """Take one unsold credential off the shelf for good. False if it was sold or held first.

    Deleted rather than marked VOID: it was never sold, so there is no history to keep — the audit
    log records who removed it — and a login removed by mistake can simply be added again.
    """
    result = await session.execute(
        delete(StockItem).where(
            StockItem.id == stock_item_id,
            StockItem.product_id == product_id,
            is_free(datetime.now(UTC)),
        )
    )
    await session.flush()
    if not result.rowcount:
        return False
    product = await ProductRepo(session).get_by_id(product_id)
    if product is not None:
        await resync_status(session, product)
        await session.flush()
    return True


def preview(payload: str, width: int = 34) -> str:
    """A one-line, plain-text glimpse of a credential, for a button label."""
    text = normalize_stock_text(payload)
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


# ---- Splitting one pasted message into several items ----
#
# A message used to be exactly one item, always — right for a multi-line login (email, password,
# 2FA code), wrong for a list of twenty logins pasted in one go, which became one "item" and sold all
# twenty to the first buyer. Neither guess is safe, so a message with more than one line is split
# only the way the admin says.

SPLIT_LINES = "l"  # every non-empty line is its own item
SPLIT_BLOCKS = "b"  # items are separated by blank lines; lines inside a block stay together
KEEP_WHOLE = "k"  # the whole message is one item (formatting kept exactly)

_BLOCK_GAP = re.compile(r"\n[ \t]*\n")
_MONOSPACE = re.compile(r"(<pre>)?(<code[^>]*>)?[^<]*(</code>)?(</pre>)?", re.S)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _blocks(text: str) -> list[str]:
    return [block.strip() for block in _BLOCK_GAP.split(text.replace("\r\n", "\n")) if block.strip()]


def split_counts(text: str) -> tuple[int, int]:
    """How many items each split would make: (one per line, one per blank-line block)."""
    return len(_lines(text)), len(_blocks(text))


def split_payload(*, text: str, html: str, mode: str) -> list[str]:
    """The items one message becomes under `mode`.

    Keeping it whole keeps the admin's formatting exactly (`html`). Split pieces are cut from the
    plain text instead — HTML tags can span lines, and cutting through one would leave every piece
    malformed — and a message that was one copy-box as a whole gives a copy-box to every piece.
    """
    if mode == KEEP_WHOLE:
        return [html]
    pieces = _lines(text) if mode == SPLIT_LINES else _blocks(text)
    monospace = ("<pre" in html or "<code" in html) and bool(_MONOSPACE.fullmatch(html))
    if not monospace:
        return [escape_html(piece) for piece in pieces]
    return [
        f"<pre>{escape_html(piece)}</pre>" if "\n" in piece else f"<code>{escape_html(piece)}</code>"
        for piece in pieces
    ]
