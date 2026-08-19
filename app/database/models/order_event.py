from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, BigIntPKMixin


class OrderEventKind(str, enum.Enum):
    """Everything that can ever happen to an order, one label each.

    The set is deliberately closed and small. An event exists when there is a question an admin or a
    buyer will later ask about the order — "was it delivered?", "why was it declined?", "where did
    the money go?" — and nothing exists just because a function ran.
    """

    PLACED = "PLACED"
    DELIVERED = "DELIVERED"
    DECLINED = "DECLINED"
    REFUND_PARKED = "REFUND_PARKED"
    REFUND_PAID_OUT = "REFUND_PAID_OUT"
    REFUND_MOVED = "REFUND_MOVED"
    TICKET_OPENED = "TICKET_OPENED"


class OrderEventActor(str, enum.Enum):
    SYSTEM = "SYSTEM"
    ADMIN = "ADMIN"
    USER = "USER"


class OrderEvent(BigIntPKMixin, Base):
    """One immutable line in an order's history.

    Rows are appended and never updated: the point of the table is that a decline reason typed six
    weeks ago still reads exactly as it was typed, and that the ID quoted in a refund conversation
    still resolves to the same event. Anything that changes belongs on `orders` instead.

    Separate from `audit_logs`, which records what an *admin* did across the whole bot. This records
    what happened to one *order*, including the things no admin did (auto-delivery, the buyer placing
    it), and it is what the order timeline and the admin search are built on.
    """

    __tablename__ = "order_events"

    # The searchable handle, e.g. DEC-1A0F73. Prefixed by kind so the ID alone says what it is —
    # somebody pasting "RFD-77C2E9" into the search bar has told us they mean a refund without
    # having to say so. Minted by `app.core.security.new_event_number`.
    event_number: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    kind: Mapped[OrderEventKind] = mapped_column(Enum(OrderEventKind, name="order_event_kind"))
    actor: Mapped[OrderEventActor] = mapped_column(
        Enum(OrderEventActor, name="order_event_actor"), default=OrderEventActor.SYSTEM
    )
    # Who, when the actor was a person. NULL for SYSTEM.
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)

    # Money the event moved, in minor units, always positive — the kind says which direction. NULL
    # for events that move nothing (PLACED, DELIVERED, TICKET_OPENED).
    amount_minor: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(8))

    # Free text the buyer is entitled to read: the decline reason, the note on a payout. Text rather
    # than String(n) because it is quoted back verbatim in two places and truncating it would make
    # the two disagree.
    reason: Mapped[str | None] = mapped_column(Text)
    # Whatever else identifies this event's counterpart — a ticket number, a wallet transaction id,
    # an on-chain tx hash. One loose field on purpose: it is only ever rendered, never queried.
    reference: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # Rows are read back by (created_at, id). Two events written inside the same transaction can
    # share a timestamp to the microsecond, and the autoincrement id is the only thing that then
    # keeps "declined" from rendering below "refund parked".
    __table_args__ = (Index("ix_order_events_order_created", "order_id", "created_at"),)
