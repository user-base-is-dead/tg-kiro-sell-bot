from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import ARRAY, JSON, BigInteger, DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, BigIntPKMixin, TimestampMixin


class TicketStatus(str, enum.Enum):
    OPEN = "OPEN"
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class TicketPriority(str, enum.Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class SupportTicket(BigIntPKMixin, TimestampMixin, Base):
    __tablename__ = "support_tickets"

    ticket_number: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(String(256))
    status: Mapped[TicketStatus] = mapped_column(Enum(TicketStatus, name="ticket_status"), default=TicketStatus.OPEN)
    priority: Mapped[TicketPriority] = mapped_column(
        Enum(TicketPriority, name="ticket_priority"), default=TicketPriority.NORMAL
    )
    topic_id: Mapped[int | None] = mapped_column(BigInteger)
    # Which chat that topic lives in. NULL means SUPPORT_GROUP_ID, which is every ordinary ticket —
    # the column exists because an order dispute is hosted by the order's own topic in
    # ORDERS_GROUP_ID instead, and the relay has to know where to post without guessing. Stored
    # rather than derived: the group id can be changed in the env, and a live ticket must keep
    # talking to the group it was actually opened in.
    group_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    # The order this ticket IS the dispute for, when it is one. Not a FK for the same reason
    # `orders.refund_ticket_id` isn't: either side can be purged while the other's record of the
    # link must survive. Its presence is what marks a ticket as an order dispute — the thing that
    # locks the user out of opening anything else until an admin runs /close.
    order_id: Mapped[str | None] = mapped_column(String(36), index=True)
    assigned_staff_id: Mapped[int | None] = mapped_column(BigInteger)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(String(512))

    __table_args__ = (Index("ix_tickets_status_opened", "status", "opened_at"),)


class TicketMessage(BigIntPKMixin, Base):
    __tablename__ = "ticket_messages"

    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id"), index=True)
    author_type: Mapped[str] = mapped_column(String(16))  # USER / STAFF / SYSTEM
    author_telegram_id: Mapped[int] = mapped_column(BigInteger)
    content: Mapped[str | None] = mapped_column(String(4096))
    # ARRAY on Postgres; plain JSON on SQLite (used by the test suite) — Postgres-specific
    # array operators aren't needed here, just storage + iteration.
    attachment_file_ids: Mapped[list[str]] = mapped_column(ARRAY(String).with_variant(JSON(), "sqlite"), default=list)
    relayed_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
