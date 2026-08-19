from __future__ import annotations

import enum
from datetime import datetime

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


class FundingSource(str, enum.Enum):
    """Where the money that paid for this order actually came from.

    Every order is paid out of the wallet — crypto tops the wallet up and the wallet buys, so
    `place_order` never sees a chain transfer. But "they pressed Pay with Crypto and sent USDT for
    this order" and "they spent a balance they already had" are different facts to the person owed a
    refund, and only the first one cannot be reversed by crediting a balance. CRYPTO is set when the
    order consumed a confirmed crypto invoice that was opened from this product's checkout.
    """

    WALLET = "WALLET"
    CRYPTO = "CRYPTO"


class RefundState(str, enum.Enum):
    """How far the money owed on a declined order has got.

    NONE is every order that was never refunded, including live ones. PARKED means the amount is
    sitting in the buyer's Refund Wallet, visible to them and to staff, spendable by nobody. SETTLED
    means an admin has accounted for all of it — paid out on chain, moved into the spendable wallet,
    or some of each. There is deliberately no automatic transition into SETTLED: a payout happens
    outside this bot, so only a human can say it happened.
    """

    NONE = "NONE"
    PARKED = "PARKED"
    SETTLED = "SETTLED"


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
    # The reason the order died, in the words of whoever ended it. Shown to the buyer verbatim and
    # kept for the life of the order — a decline whose reason is not on the order is a decline
    # nobody can explain a month later.
    failure_reason: Mapped[str | None] = mapped_column(String(512))
    cancelled_by_admin_id: Mapped[int | None] = mapped_column(BigInteger)

    funding_source: Mapped[FundingSource] = mapped_column(
        Enum(FundingSource, name="funding_source"), default=FundingSource.WALLET
    )
    # The confirmed crypto invoice this order consumed, when it was bought on chain. Kept so a refund
    # conversation can quote the transaction the buyer will be looking at in their own wallet.
    crypto_payment_id: Mapped[int | None] = mapped_column(BigInteger)

    refund_state: Mapped[RefundState] = mapped_column(
        Enum(RefundState, name="refund_state"), default=RefundState.NONE
    )
    refund_amount_minor: Mapped[int | None] = mapped_column(Integer)
    # The thread the refund is being settled in. Not a FK: a ticket can be purged while the order's
    # record of having had one must survive.
    refund_ticket_id: Mapped[int | None] = mapped_column(BigInteger)

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")

    __table_args__ = (Index("ix_orders_user_placed", "user_id", "placed_at"),)


class OrderItem(BigIntPKMixin, Base):
    __tablename__ = "order_items"

    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    # Nullable so an admin can delete a product outright without taking buyers' order history with
    # it. Everything this row needs to render on its own — name, price, warranty — is snapshotted
    # below, so a NULL here costs the link back to the catalog and nothing else.
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    product_name: Mapped[str] = mapped_column(String(128))
    unit_price_minor: Mapped[int] = mapped_column(Integer)
    qty: Mapped[int] = mapped_column(Integer, default=1)
    warranty_days: Mapped[int] = mapped_column(Integer, default=0)

    # Eager for the same reason as `Warranty.order_item`: the parent order's number and currency are
    # needed wherever an item is rendered, and an async lazy load there would raise.
    order: Mapped["Order"] = relationship(back_populates="items", lazy="selectin")


# `OrderHold` used to live here: one row per (product, user) reserving the WHOLE product for five
# minutes. That was the wrong grain — a product holds many independent credentials, and reserving
# the product made all of them look unavailable while only one was being bought. Reservations now
# live on the `stock_items` row itself (`status=HELD`, `held_by_user_id`, `held_until`), so one
# buyer's hold takes exactly one credential off the shelf. See `app/services/stock_hold_service.py`.
# The table is dropped in migration 0014.


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
    # Set when the user files a claim: the moment the staff-response clock starts, and the
    # support ticket the claim conversation lives in (see jobs/warranty_auto_reject.py).
    claim_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_ticket_id: Mapped[int | None] = mapped_column(ForeignKey("support_tickets.id"), index=True)

    # Two clocks, deliberately separate. `expires_at` above is the product's warranty and is never
    # rewritten by the claim process — it is the source of truth even while a claim sits under
    # review. `claim_deadline_at` is how long staff have to answer the claim; blowing through it
    # auto-rejects the claim and says nothing about whether the warranty itself is still alive.
    claim_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Warranty time left at the instant the claim was filed, frozen. This is what a successful
    # /done grants to the replacement item (`done_time + claim_remaining_seconds`). It is a
    # snapshot for the payout calculation only — the underlying warranty keeps running against
    # `expires_at` regardless, so a claim resolved after `expires_at` grants nothing.
    claim_remaining_seconds: Mapped[int | None] = mapped_column(Integer)
    claim_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Async sessions cannot lazy-load on attribute access, and every consumer of a warranty needs
    # the product name off the item, so it is loaded up front rather than being an AttributeError
    # waiting to happen.
    order_item: Mapped["OrderItem"] = relationship(lazy="selectin")

    __table_args__ = (Index("ix_warranties_expires_status", "expires_at", "status"),)
