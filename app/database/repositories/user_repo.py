from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.user import User


def _new_referral_code() -> str:
    return secrets.token_urlsafe(6).replace("_", "").replace("-", "")[:8].upper()


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: int) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        result = await self._session.execute(select(User).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()

    async def get_by_referral_code(self, code: str) -> User | None:
        result = await self._session.execute(select(User).where(User.referral_code == code))
        return result.scalar_one_or_none()

    async def list_page(self, *, offset: int, limit: int) -> list[User]:
        """Newest signup first, so page 1 is who just arrived and the oldest members sit on the
        last page. An admin opens this screen to look at recent activity, and burying that behind
        every page of history made the screen useless on a store with thousands of users.

        Ordered by id as a tiebreak because first_seen_at carries a server default and several rows
        can share a timestamp on a bulk insert — without it, paging could repeat or drop rows."""
        stmt = (
            select(User).order_by(User.first_seen_at.desc(), User.id.desc()).offset(offset).limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_all(self) -> int:
        result = await self._session.execute(select(func.count()).select_from(User))
        return int(result.scalar_one())

    async def search(self, query: str, limit: int = 10) -> list[User]:
        query = query.strip().lstrip("@")
        if query.isdigit():
            stmt = select(User).where(User.telegram_id == int(query))
        else:
            stmt = select(User).where(User.username.ilike(f"%{query}%")).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def upsert_from_telegram(
        self,
        *,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        chat_id: int | None,
        referred_by_id: int | None = None,
        default_locale: str = "en",
    ) -> tuple[User, bool]:
        user = await self.get_by_telegram_id(telegram_id)
        now = datetime.now(UTC)
        is_new = user is None

        if is_new:
            code = _new_referral_code()
            while await self.get_by_referral_code(code) is not None:
                code = _new_referral_code()

            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                chat_id=chat_id,
                locale=default_locale,
                referral_code=code,
                referred_by_id=referred_by_id,
                first_seen_at=now,
                last_seen_at=now,
            )
            self._session.add(user)
            await self._session.flush()
        else:
            user.username = username
            user.first_name = first_name
            user.last_name = last_name
            if chat_id is not None:
                user.chat_id = chat_id
            user.last_seen_at = now

        return user, is_new
