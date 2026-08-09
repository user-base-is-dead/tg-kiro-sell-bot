from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.commands import clear_admin_commands, register_bot_commands
from app.bot.handlers.admin.balance_adjust import router as admin_balance_adjust_router
from app.bot.handlers.admin.broadcast import router as admin_broadcast_router
from app.bot.handlers.admin.categories import router as admin_categories_router
from app.bot.handlers.admin.dashboard import router as admin_dashboard_router
from app.bot.handlers.admin.gifts import router as admin_gifts_router
from app.bot.handlers.admin.guard import router as admin_guard_router
from app.bot.handlers.admin.logs import router as admin_logs_router
from app.bot.handlers.admin.orders import router as admin_orders_router
from app.bot.handlers.admin.panel import router as admin_panel_router
from app.bot.handlers.admin.payments import router as admin_payments_router
from app.bot.handlers.admin.products import router as admin_products_router
from app.bot.handlers.admin.settings import router as admin_settings_router
from app.bot.handlers.admin.support import router as admin_support_router
from app.bot.handlers.admin.users import router as admin_users_router
from app.bot.handlers.gifts.redeem import router as gifts_redeem_router
from app.bot.handlers.nav import router as nav_router
from app.bot.handlers.orders.checkout import router as orders_checkout_router
from app.bot.handlers.orders.history import router as orders_history_router
from app.bot.handlers.payments.topup import router as payments_topup_router
from app.bot.handlers.payments.topup_crypto import router as payments_topup_crypto_router
from app.bot.handlers.products.browse import router as products_browse_router
from app.bot.handlers.referrals.screen import router as referrals_screen_router
from app.bot.handlers.support.create import router as support_create_router
from app.bot.handlers.support.my_tickets import router as support_my_tickets_router
from app.bot.handlers.support.relay import router as support_relay_router
from app.bot.handlers.user.help import router as help_router
from app.bot.handlers.user.menu_placeholders import router as menu_placeholders_router
from app.bot.handlers.user.profile import router as profile_router
from app.bot.handlers.user.start import router as start_router
from app.bot.handlers.warranty.claim import router as warranty_claim_router
from app.bot.handlers.warranty.screen import router as warranty_router
from app.bot.handlers.admin.warranty_claims import router as admin_warranty_claims_router
from app.bot.middlewares.ban_check import BanCheckMiddleware
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.bot.middlewares.error import ErrorMiddleware
from app.bot.middlewares.support_exit_notice import SupportExitNoticeMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.bot.middlewares.user import UserMiddleware
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.redis import build_redis
from app.database.repositories.admin_repo import AdminRepo
from app.database.repositories.audit_repo import AuditRepo
from app.database.session import build_engine, build_sessionmaker, session_scope
from app.jobs.broadcast_worker import resume_interrupted_broadcasts
from app.jobs.scheduler import build_scheduler

logger = logging.getLogger(__name__)


def _include_routers(dp: Dispatcher) -> None:
    # Order matters: admin panel before generic placeholders isn't required here since
    # custom_id/text values never collide across routers, but registration order is kept
    # deterministic for readability as more domains are added in later phases.
    dp.include_router(start_router)
    dp.include_router(help_router)
    dp.include_router(admin_panel_router)
    dp.include_router(admin_categories_router)
    dp.include_router(admin_products_router)
    dp.include_router(admin_orders_router)
    dp.include_router(admin_payments_router)
    dp.include_router(admin_balance_adjust_router)
    dp.include_router(admin_gifts_router)
    dp.include_router(admin_support_router)
    dp.include_router(admin_dashboard_router)
    dp.include_router(admin_users_router)
    dp.include_router(admin_settings_router)
    dp.include_router(admin_logs_router)
    dp.include_router(admin_broadcast_router)
    dp.include_router(admin_warranty_claims_router)
    # Must sit after every admin router (admins are already served above) and before the
    # generic nav/catch-all routers, so a non-admin gets an explicit "not authorized" instead
    # of silence or a misleading "unknown action".
    dp.include_router(admin_guard_router)
    dp.include_router(products_browse_router)
    dp.include_router(orders_checkout_router)
    dp.include_router(orders_history_router)
    dp.include_router(payments_topup_router)
    dp.include_router(payments_topup_crypto_router)
    dp.include_router(profile_router)
    dp.include_router(warranty_router)
    dp.include_router(warranty_claim_router)
    dp.include_router(gifts_redeem_router)
    dp.include_router(referrals_screen_router)
    dp.include_router(support_create_router)
    dp.include_router(support_my_tickets_router)
    dp.include_router(nav_router)
    dp.include_router(menu_placeholders_router)
    dp.include_router(support_relay_router)  # catch-all: must stay last


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = build_engine(settings.database_url, echo=not settings.is_production)
    sessionmaker = build_sessionmaker(engine)

    async with session_scope(sessionmaker) as session:
        revoked = await AdminRepo(session).sync_env_owners(settings.admin_ids)
        if revoked:
            logger.warning("Revoked admin access for ids no longer in ADMIN_IDS: %s", revoked)
            await AuditRepo(session).log(
                actor_telegram_id=0,
                action="security.admin_revoked_on_boot",
                target_type="env",
                target_id=",".join(str(i) for i in revoked)[:64],
                metadata={"revoked": revoked, "admin_ids": settings.admin_ids},
            )

    redis = build_redis(settings.redis_url)
    storage = RedisStorage(redis=redis)

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=storage)

    # withErrorBoundary -> withRateLimit -> withUser -> withAuth(per-handler) -> handler
    dp.update.middleware(ErrorMiddleware(bot, settings.log_chat_id))
    dp.update.middleware(ThrottlingMiddleware(redis))
    dp.update.middleware(DbSessionMiddleware(sessionmaker))
    dp.update.middleware(UserMiddleware(default_locale=settings.default_locale))
    dp.update.middleware(BanCheckMiddleware())

    # Inner, not outer, and on `message` rather than `update`: it has to read data["handler"],
    # which only exists once aiogram has resolved which handler matched.
    dp.message.middleware(SupportExitNoticeMiddleware())

    _include_routers(dp)

    scheduler = build_scheduler(sessionmaker, bot)
    scheduler.start()

    try:
        if revoked:
            await clear_admin_commands(bot, revoked)
        await register_bot_commands(bot, settings.admin_ids)
        logger.info("Registered bot commands for %d admin(s)", len(settings.admin_ids))
    except Exception as exc:
        logger.error("Failed to register bot commands: %s", exc)

    # Resumes any broadcast left RUNNING by a previous process (crash/restart) before
    # accepting new traffic — PENDING deliveries pick up exactly where they left off.
    await resume_interrupted_broadcasts(bot, settings.database_url, sessionmaker)

    logger.info("Starting bot polling (environment=%s)", settings.environment)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await redis.aclose()
        await engine.dispose()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
