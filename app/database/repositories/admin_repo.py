from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.admin import Admin, AdminRole


class AdminRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_telegram_id(self, telegram_id: int) -> Admin | None:
        result = await self._session.execute(select(Admin).where(Admin.telegram_id == telegram_id))
        return result.scalar_one_or_none()

    async def sync_env_owners(self, owner_ids: list[int]) -> list[int]:
        """Makes the admins table match ADMIN_IDS, in BOTH directions. Runs once at boot — env ids
        are always OWNER-equivalent even if this table is empty or was tampered with
        (lockout-proof floor).

        The revoke half matters as much as the grant half: dropping an id from ADMIN_IDS used to
        leave its row behind forever, so the removed account still passed IsAdmin (which falls
        back to this table) and kept full admin access across restarts.

        Only env-seeded rows (granted_by_id IS NULL) are revoked here. Admins granted from inside
        the bot carry their granter's id and are owned by that flow, not by env config.

        Returns the telegram ids that were revoked, so the caller can log them.
        """
        wanted = set(owner_ids)

        result = await self._session.execute(select(Admin).where(Admin.granted_by_id.is_(None)))
        seeded = list(result.scalars().all())

        revoked: list[int] = []
        for admin in seeded:
            if admin.telegram_id not in wanted:
                await self._session.delete(admin)
                revoked.append(admin.telegram_id)
            elif admin.role != AdminRole.OWNER:
                admin.role = AdminRole.OWNER

        present = {admin.telegram_id for admin in seeded}
        for telegram_id in wanted - present:
            existing = await self.get_by_telegram_id(telegram_id)
            if existing is None:
                self._session.add(Admin(telegram_id=telegram_id, role=AdminRole.OWNER, granted_by_id=None))
            elif existing.role != AdminRole.OWNER:
                existing.role = AdminRole.OWNER

        await self._session.flush()
        return revoked
