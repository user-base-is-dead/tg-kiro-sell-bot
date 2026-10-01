from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, BigIntPKMixin, TimestampMixin


class ProductStatus(str, enum.Enum):
    IN_STOCK = "IN_STOCK"
    LOW_STOCK = "LOW_STOCK"
    # Every remaining credential is held by someone mid-checkout. Distinct from OUT_OF_STOCK on
    # purpose: those holds expire, so this state un-does itself within five minutes and the buyer
    # should be told to wait rather than turned away.
    ON_HOLD = "ON_HOLD"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    COMING_SOON = "COMING_SOON"
    DISABLED = "DISABLED"


class FulfillmentMode(str, enum.Enum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class StockStatus(str, enum.Enum):
    """The lifecycle of ONE credential. Availability is counted per credential, never per product.

    AVAILABLE → HELD  — a buyer picked a payment method; this exact credential is theirs for 5 min
    HELD  → AVAILABLE — they backed out, or the hold expired without payment
    HELD  → RESERVED  — payment is being taken, inside the order transaction
    RESERVED → DELIVERED — handed over; permanently that buyer's
    RESERVED → AVAILABLE — the order was cancelled/refunded before delivery
    """

    AVAILABLE = "AVAILABLE"
    HELD = "HELD"
    RESERVED = "RESERVED"
    DELIVERED = "DELIVERED"
    VOID = "VOID"


class Category(BigIntPKMixin, TimestampMixin, Base):
    __tablename__ = "categories"

    name: Mapped[str] = mapped_column(String(128))
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(1024))
    emoji: Mapped[str | None] = mapped_column(String(16))
    image_file_id: Mapped[str | None] = mapped_column(String(256))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(BigIntPKMixin, TimestampMixin, Base):
    __tablename__ = "products"

    # Nullable: a product can sit outside every category. Picking "no category" used to fabricate a
    # real Category row named "Uncategorized", which then showed up as a folder in the buyer store.
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(128))
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(2048))
    price_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    status: Mapped[ProductStatus] = mapped_column(
        Enum(ProductStatus, name="product_status"), default=ProductStatus.OUT_OF_STOCK
    )
    fulfillment_mode: Mapped[FulfillmentMode] = mapped_column(
        Enum(FulfillmentMode, name="fulfillment_mode"), default=FulfillmentMode.AUTO
    )
    # Admin-set sellable count, overriding "however many credentials are loaded". NULL = no
    # override. When set it is the number shoppers see and the number that gets decremented on
    # every sale; the credential pool still decides *how* each order is delivered (see
    # order_service.resolve_fulfillment).
    manual_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=3)
    image_file_id: Mapped[str | None] = mapped_column(String(256))
    thumbnail_file_id: Mapped[str | None] = mapped_column(String(256))
    delivery_info: Mapped[str | None] = mapped_column(String(2048))
    warranty_days: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(String(1024))
    max_per_user: Mapped[int | None] = mapped_column(Integer)
    # Opt out of "one login, one sale" for this product only.
    #
    # The default is the rule the store is built on: a credential reaches exactly one buyer, enforced
    # by the unique index on `StockItem.content_hash` below. That is wrong for a *shared* account —
    # one login the shop deliberately sells to many people — where every copy on the shelf is the
    # same text and the fingerprint rejects all but the first.
    #
    # When this is on, stock added to this product is stored with `content_hash = NULL`: NULL is
    # exempt from a unique index on both SQLite and Postgres, so unlimited copies can sit side by
    # side while the guarantee stays fully intact for every product that did not opt out. Per
    # product rather than global precisely so turning it on for one shared account cannot quietly
    # let a single-use licence key be sold twice.
    allow_duplicate_stock: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    category: Mapped["Category | None"] = relationship(back_populates="products")
    stock_items: Mapped[list["StockItem"]] = relationship(back_populates="product")

    __table_args__ = (Index("ix_products_category_active", "category_id", "is_active"),)


class StockItem(BigIntPKMixin, Base):
    __tablename__ = "stock_items"

    # Nullable for the same reason as `OrderItem.product_id`: deleting a product must not destroy an
    # already-delivered payload, which the buyer can still be shown under warranty. Unsold rows are
    # deleted outright with the product — only sold ones survive, detached.
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), index=True)
    payload: Mapped[str] = mapped_column(String(4096))  # encrypted at rest via PayloadCipher
    # `security.stock_fingerprint` of the plaintext: the one thing that recognises the same login
    # when it is uploaded twice, because the ciphertext above differs on every encryption. Unique
    # across the whole table, sold rows included — a login that has already reached one buyer must
    # never be put on sale again, in this product or any other. NULL only for rows that predate it
    # and could not be fingerprinted, were duplicates already sold (see migration 0027), or belong
    # to a product with `allow_duplicate_stock` on — a shared account, where many copies of one
    # login are the point. NULL is exempt from the unique index on every dialect we run, which is
    # exactly how that opt-out is expressed without weakening the index for anybody else.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[StockStatus] = mapped_column(
        Enum(StockStatus, name="stock_status"), default=StockStatus.AVAILABLE
    )
    order_item_id: Mapped[int | None] = mapped_column(ForeignKey("order_items.id"))
    batch_id: Mapped[str | None] = mapped_column(String(64))
    added_by_admin_id: Mapped[int | None] = mapped_column(BigInteger)

    # The reservation lives on the credential itself rather than in a side table, so there is
    # exactly one row — and therefore one source of truth — for "who has this, until when". A hold
    # kept anywhere else can disagree with `status`, and a credential that is HELD in one place and
    # AVAILABLE in another is precisely how the same login reaches two customers.
    held_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    held_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    product: Mapped["Product | None"] = relationship(back_populates="stock_items")

    __table_args__ = (
        Index("ix_stock_items_product_status", "product_id", "status"),
        # The expiry sweep is `WHERE status = 'HELD' AND held_until <= now`, on every tick.
        Index("ix_stock_items_status_held_until", "status", "held_until"),
        # The database's own guarantee that one login is stored — and so sold — once.
        Index("ux_stock_items_content_hash", "content_hash", unique=True),
    )
