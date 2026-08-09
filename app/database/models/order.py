from __future__ import annotations

import enum
from datetime import UTC, datetime, timedelta

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, BigIntPKMixin, TimestampMixin, new_uuid


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class WarrantyStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CLAIMED = "CLAIMED"
    VOID = "VOID"


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_uuid)
    order_number: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus, name="order_status"), default=OrderStatus.PENDING)
    subtotal_minor: Mapped[int] = mapped_column(Integer)
    discount_minor: Mapped[int] = mapped_column(Integer, default=0)
    total_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    payment_txn_id: Mapped[int | None] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(512))

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")

    __table_args__ = (Index("ix_orders_user_placed", "user_id", "placed_at"),)


class OrderItem(BigIntPKMixin, Base):
    __tablename__ = "order_items"

    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    product_name: Mapped[str] = mapped_column(String(128))
    unit_price_minor: Mapped[int] = mapped_column(Integer)
    qty: Mapped[int] = mapped_column(Integer, default=1)
    warranty_days: Mapped[int] = mapped_column(Integer, default=0)

    order: Mapped["Order"] = relationship(back_populates="items")


class OrderHold(BigIntPKMixin, Base):
    """Tracks product reservations during 5-minute payment window."""

    __tablename__ = "order_holds"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    held_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC) + timedelta(minutes=5)
    )
    order_id: Mapped[str | None] = mapped_column(String(36), index=True)

    __table_args__ = (Index("ix_order_holds_product_expires", "product_id", "expires_at"),)


class Delivery(BigIntPKMixin, Base):
    __tablename__ = "deliveries"

    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id"), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str | None] = mapped_column(String(4096))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_by_admin_id: Mapped[int | None] = mapped_column(BigInteger)
    delivery_message_id: Mapped[int | None] = mapped_column(BigInteger)


class Warranty(BigIntPKMixin, Base):
    __tablename__ = "warranties"

    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id"), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[WarrantyStatus] = mapped_column(
        Enum(WarrantyStatus, name="warranty_status"), default=WarrantyStatus.ACTIVE
    )
    claim_notes: Mapped[str | None] = mapped_column(String(1024))

    __table_args__ = (Index("ix_warranties_expires_status", "expires_at", "status"),)
