from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.jobs.crypto_payment_checker import check_crypto_payments
from app.jobs.hold_expiry import expire_stock_holds
from app.jobs.ticket_archival import archive_stale_tickets
from app.jobs.warranty_auto_reject import auto_reject_expired_warranty_claims
from app.jobs.warranty_expiry import expire_warranties

logger = logging.getLogger(__name__)


def build_scheduler(sessionmaker: async_sessionmaker, bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    # Neither warranty job carries correctness on its own — every read derives status from the
    # stored timestamps, so a missed run only leaves a stale `status` column, never a warranty that
    # outlives its expiry. They keep the persisted state honest and send the notifications.
    scheduler.add_job(expire_warranties, "interval", hours=1, args=[sessionmaker], id="warranty_expiry", coalesce=True)
    scheduler.add_job(
        auto_reject_expired_warranty_claims,
        "interval",
        minutes=15,
        args=[sessionmaker, bot],
        id="warranty_auto_reject",
        coalesce=True,
    )
    scheduler.add_job(
        archive_stale_tickets, "interval", hours=1, args=[sessionmaker, bot], id="ticket_archival", coalesce=True
    )
    scheduler.add_job(
        check_crypto_payments, "interval", seconds=30, args=[sessionmaker], id="crypto_payments", coalesce=True
    )
    # Frequent because a freed credential is a sale waiting to happen, and cheap because it is one
    # indexed UPDATE. Correctness does not ride on the interval — every read already treats a lapsed
    # hold as available — so this only keeps the stored state from drifting.
    scheduler.add_job(
        expire_stock_holds, "interval", seconds=30, args=[sessionmaker], id="stock_hold_expiry", coalesce=True
    )
    return scheduler
