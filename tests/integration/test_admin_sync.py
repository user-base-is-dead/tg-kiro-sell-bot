from __future__ import annotations

from app.bot.filters.is_admin import is_admin_user
from app.database.models.admin import Admin, AdminRole
from app.database.repositories.admin_repo import AdminRepo

# Synthetic ids on purpose: is_admin_user() consults the real ADMIN_IDS as its lockout-proof
# floor, so a test id must be one that can never appear there.
OLD = 900000000001
NEW = 900000000002


async def test_id_dropped_from_env_loses_admin(sqlite_sessionmaker) -> None:
    """The bug this exists for: an id removed from ADMIN_IDS kept its admins row, and IsAdmin
    falls back to that table — so the old account stayed a full admin across restarts."""
    async with sqlite_sessionmaker() as session:
        await AdminRepo(session).sync_env_owners([OLD])
        await session.commit()
        assert await is_admin_user(session, OLD) is True

    async with sqlite_sessionmaker() as session:
        revoked = await AdminRepo(session).sync_env_owners([NEW])
        await session.commit()
        assert revoked == [OLD]

    async with sqlite_sessionmaker() as session:
        assert await AdminRepo(session).get_by_telegram_id(OLD) is None
        assert await AdminRepo(session).get_by_telegram_id(NEW) is not None


async def test_sync_is_idempotent(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        repo = AdminRepo(session)
        assert await repo.sync_env_owners([NEW]) == []
        assert await repo.sync_env_owners([NEW]) == []
        await session.commit()

    async with sqlite_sessionmaker() as session:
        admin = await AdminRepo(session).get_by_telegram_id(NEW)
        assert admin is not None and admin.role == AdminRole.OWNER


async def test_in_bot_granted_admins_survive_env_sync(sqlite_sessionmaker) -> None:
    """Only env-seeded rows (granted_by_id IS NULL) are env-owned. A future in-bot grant flow
    must not be wiped every time the process restarts."""
    async with sqlite_sessionmaker() as session:
        session.add(Admin(telegram_id=999, role=AdminRole.SUPPORT, granted_by_id=NEW))
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await AdminRepo(session).sync_env_owners([NEW]) == []
        await session.commit()

    async with sqlite_sessionmaker() as session:
        granted = await AdminRepo(session).get_by_telegram_id(999)
        assert granted is not None
        assert granted.role == AdminRole.SUPPORT  # not force-promoted to OWNER either


async def test_role_is_restored_for_a_tampered_env_owner(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        session.add(Admin(telegram_id=NEW, role=AdminRole.SUPPORT, granted_by_id=None))
        await session.commit()

    async with sqlite_sessionmaker() as session:
        assert await AdminRepo(session).sync_env_owners([NEW]) == []
        await session.commit()

    async with sqlite_sessionmaker() as session:
        admin = await AdminRepo(session).get_by_telegram_id(NEW)
        assert admin is not None and admin.role == AdminRole.OWNER
