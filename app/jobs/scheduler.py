from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.jobs.crypto_payment_checker import check_crypto_payments
from app.jobs.ticket_archival import archive_stale_tickets
from app.jobs.warranty_expiry import expire_warranties

logger = logging.getLogger(__name__)


def build_scheduler(sessionmaker: async_sessionmaker, bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(expire_warranties, "interval", hours=1, args=[sessionmaker], id="warranty_expiry", coalesce=True)
    scheduler.add_job(
        archive_stale_tickets, "interval", hours=1, args=[sessionmaker, bot], id="ticket_archival", coalesce=True
    )
    scheduler.add_job(
        check_crypto_payments, "interval", seconds=30, args=[sessionmaker], id="crypto_payments", coalesce=True
    )
    return scheduler
