from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, BigIntPKMixin, TimestampMixin


class GiftStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    EXPIRED = "EXPIRED"
    EXHAUSTED = "EXHAUSTED"


class GiftKind(str, enum.Enum):
    CREDIT = "CREDIT"
    # The gift carries its own items, pasted when the code is created. There used to be a PRODUCT
    # kind that handed out a catalog product and consumed its stock; a giveaway is not a sale, and
    # tying the two meant a promo could quietly empty the shelf a paying customer was queueing for.
    # Gift stock is now entirely separate — see `GiftItem`.
    ITEM = "ITEM"


class GiftCode(BigIntPKMixin, TimestampMixin, Base):
    """A code that grants exactly one thing: wallet credit *or* its own attached items.

    `kind` is the discriminator. CREDIT fills `value_minor`; ITEM owns rows in `gift_items` and
    leaves `value_minor` NULL. Nothing enforces that pairing at the DB level (SQLite can't add a
    CHECK to an existing table without a full rebuild), so `create_gift_code` is the single gate
    that guarantees it.

    Gift stock never touches the catalog. `stock_items` belongs to products people pay for, and a
    giveaway must not be able to consume it.
    """

    __tablename__ = "gift_codes"

    code_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    code_last4: Mapped[str] = mapped_column(String(4))
    kind: Mapped[GiftKind] = mapped_column(Enum(GiftKind, name="gift_kind"), default=GiftKind.CREDIT)
    value_minor: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[GiftStatus] = mapped_column(Enum(GiftStatus, name="gift_status"), default=GiftStatus.ACTIVE)
    created_by_admin_id: Mapped[int] = mapped_column(BigInteger)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str | None] = mapped_column(String(512))


class GiftRedemption(BigIntPKMixin, Base):
    __tablename__ = "gift_redemptions"

    gift_code_id: Mapped[int] = mapped_column(ForeignKey("gift_codes.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # A CREDIT claim moves money and points at the transaction. An ITEM claim moves nothing but a
    # gift item, so both stay NULL and `gift_items.claimed_by_user_id` records who got what.
    wallet_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("wallet_transactions.id"))
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    redeemed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ux_gift_redemptions_code_user", "gift_code_id", "user_id", unique=True),)


class GiftItemStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    DELIVERED = "DELIVERED"


class GiftItem(BigIntPKMixin, Base):
    """One giveaway credential, belonging to one gift code. Never a catalog product.

    The admin pastes these when creating the code, so a giveaway has its own pool with its own
    count and cannot draw down `stock_items`. Each row goes to at most one claimer: `_grant_item`
    claims one with a conditional UPDATE, the same way credentials are held, so two people hitting
    Claim at once cannot be handed the same line.
    """

    __tablename__ = "gift_items"

    gift_code_id: Mapped[int] = mapped_column(ForeignKey("gift_codes.id"), index=True)
    payload: Mapped[str] = mapped_column(String(4096))  # encrypted at rest via PayloadCipher
    status: Mapped[GiftItemStatus] = mapped_column(
        Enum(GiftItemStatus, name="gift_item_status"), default=GiftItemStatus.AVAILABLE
    )
    claimed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_gift_items_code_status", "gift_code_id", "status"),)
