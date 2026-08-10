"""Warranty state derived from stored timestamps, never from a running timer.

The whole system rests on one rule: `Warranty.expires_at` is an absolute instant written at
purchase, and it is the only authority on whether a warranty is alive. Nothing here decrements a
counter, so nothing here cares whether the bot was up. A process that was offline for six hours
comes back and computes exactly the same answer it would have computed had it never stopped.

Filing a claim does not pause that clock. It freezes what the *customer sees* (the claim screen
shows the remainder as it stood when they filed) and it starts a second, unrelated clock for staff
to respond. If the warranty lapses while a claim is under review, it has lapsed — the review does
not buy time back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.database.models.order import Warranty, WarrantyStatus
from app.utils.time import as_utc

# How long staff have to answer a filed claim before it auto-rejects. Distinct from any product's
# warranty length — see the `claim_deadline_at` comment on the model.
CLAIM_GRACE = timedelta(hours=24)


def now_utc() -> datetime:
    return datetime.now(UTC)


def remaining_seconds(warranty: Warranty, at: datetime | None = None) -> int:
    """Warranty time left at `at`, clamped at zero. Purely a function of stored state."""
    at = at or now_utc()
    return max(0, int((as_utc(warranty.expires_at) - at).total_seconds()))


def is_expired(warranty: Warranty, at: datetime | None = None) -> bool:
    return remaining_seconds(warranty, at) == 0


def effective_status(warranty: Warranty, at: datetime | None = None) -> WarrantyStatus:
    """The status implied by the timestamps, which may be ahead of the stored `status` column.

    `status` is only advanced to EXPIRED by an hourly sweep, so between the moment a warranty
    lapses and the next run the column still reads ACTIVE. Anything user-facing has to go through
    here or it will cheerfully print "0 remaining — ACTIVE".
    """
    if warranty.status is WarrantyStatus.ACTIVE and is_expired(warranty, at):
        return WarrantyStatus.EXPIRED
    return warranty.status


def format_duration(seconds: int) -> str:
    """`21h`, `3d 4h`, `45m` — the countdown as the customer sees it."""
    if seconds <= 0:
        return "expired"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def display_remaining(warranty: Warranty, at: datetime | None = None) -> str:
    """What to print next to a warranty in a list.

    A CLAIMED warranty shows the frozen figure captured when the claim was filed, because that is
    the amount the customer stands to get back from a successful resolution. The live figure kept
    ticking underneath it either way.
    """
    if warranty.status is WarrantyStatus.CLAIMED:
        frozen = warranty.claim_remaining_seconds
        if frozen is None:
            frozen = remaining_seconds(warranty, at)
        return f"{format_duration(frozen)} (on hold)"
    if warranty.status is not WarrantyStatus.ACTIVE:
        return "—"
    return format_duration(remaining_seconds(warranty, at))


@dataclass(frozen=True)
class ClaimOutcome:
    """Result of resolving a claim, for the caller to turn into user-facing text."""

    granted_seconds: int
    new_expires_at: datetime | None


def open_claim(warranty: Warranty, *, ticket_id: int, at: datetime | None = None) -> None:
    """Move an ACTIVE warranty into CLAIMED, freezing the payout figure and starting the staff clock.

    `expires_at` is deliberately left alone.
    """
    at = at or now_utc()
    warranty.status = WarrantyStatus.CLAIMED
    warranty.claim_started_at = at
    warranty.claim_ticket_id = ticket_id
    warranty.claim_remaining_seconds = remaining_seconds(warranty, at)
    warranty.claim_deadline_at = at + CLAIM_GRACE
    warranty.claim_resolved_at = None
    warranty.claim_rejected_at = None


def resolve_claim(warranty: Warranty, *, at: datetime | None = None) -> ClaimOutcome:
    """`/done`: staff supplied a replacement, so hand back the warranty time the claim froze.

    The new expiry runs from the moment of resolution, not from the original purchase — the
    customer could not use the product while it was being replaced, so `done_time + frozen` is what
    they get. A claim resolved after the original warranty had already lapsed grants nothing; the
    frozen figure is a cap on the payout, not a promise of one.
    """
    at = at or now_utc()
    granted = warranty.claim_remaining_seconds or 0
    if is_expired(warranty, at):
        granted = 0

    warranty.claim_resolved_at = at
    warranty.claim_notes = None

    if granted <= 0:
        warranty.status = WarrantyStatus.EXPIRED
        return ClaimOutcome(granted_seconds=0, new_expires_at=None)

    warranty.status = WarrantyStatus.ACTIVE
    warranty.starts_at = at
    warranty.expires_at = at + timedelta(seconds=granted)
    warranty.claim_remaining_seconds = None
    warranty.claim_deadline_at = None
    return ClaimOutcome(granted_seconds=granted, new_expires_at=warranty.expires_at)


def reject_claim(warranty: Warranty, *, reason: str, at: datetime | None = None) -> ClaimOutcome:
    """`/reject` (and auto-rejection): drop the claim and fall back to the original timeline.

    Nothing is restarted or extended. The warranty is checked against the `expires_at` it has had
    since purchase: still in the future and it returns to ACTIVE with that same instant; already
    past and it is simply EXPIRED. Time spent under review is not credited back, which is the whole
    point — a pending claim never paused the warranty.
    """
    at = at or now_utc()
    left = remaining_seconds(warranty, at)

    warranty.claim_rejected_at = at
    warranty.claim_notes = reason
    warranty.claim_remaining_seconds = None
    warranty.claim_deadline_at = None
    warranty.status = WarrantyStatus.ACTIVE if left > 0 else WarrantyStatus.EXPIRED

    return ClaimOutcome(granted_seconds=left, new_expires_at=as_utc(warranty.expires_at) if left else None)
