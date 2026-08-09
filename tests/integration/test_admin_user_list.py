from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.database.models.user import User, UserStatus
from app.database.repositories.user_repo import UserRepo


async def _seed(sessionmaker, count: int) -> None:
    """Inserted newest-first on purpose, so an ordering bug cannot pass by accident."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    async with sessionmaker() as session:
        for i in reversed(range(count)):
            session.add(
                User(
                    telegram_id=1000 + i,
                    username=f"user{i:03d}",
                    first_name=f"User {i}",
                    referral_code=f"REF{i:05d}",
                    first_seen_at=base + timedelta(days=i),
                    last_seen_at=base + timedelta(days=i),
                )
            )
        await session.commit()


async def test_users_are_listed_oldest_first(sqlite_sessionmaker) -> None:
    """Asked for explicitly: top of the list is the oldest member, bottom is the newest."""
    await _seed(sqlite_sessionmaker, 5)

    async with sqlite_sessionmaker() as session:
        page = await UserRepo(session).list_page(offset=0, limit=20)

    assert [u.username for u in page] == ["user000", "user001", "user002", "user003", "user004"]


async def test_a_page_holds_twenty_and_the_next_page_continues(sqlite_sessionmaker) -> None:
    """20 per page with working next/previous."""
    await _seed(sqlite_sessionmaker, 45)

    async with sqlite_sessionmaker() as session:
        repo = UserRepo(session)
        assert await repo.count_all() == 45

        first = await repo.list_page(offset=0, limit=20)
        second = await repo.list_page(offset=20, limit=20)
        third = await repo.list_page(offset=40, limit=20)

    assert len(first) == 20
    assert len(second) == 20
    assert len(third) == 5, "the last page holds the remainder"

    assert first[0].username == "user000", "page 1 starts at the oldest"
    assert second[0].username == "user020", "page 2 picks up where page 1 stopped"
    assert third[-1].username == "user044", "the last row is the newest member"

    seen = [u.username for u in first + second + third]
    assert len(set(seen)) == 45, "a paging bug would repeat or drop rows"


async def test_counting_ignores_paging(sqlite_sessionmaker) -> None:
    """The header states the real total, not how many fit on this page."""
    await _seed(sqlite_sessionmaker, 23)

    async with sqlite_sessionmaker() as session:
        assert await UserRepo(session).count_all() == 23


async def test_banned_users_are_still_listed(sqlite_sessionmaker) -> None:
    """A banned account is exactly the one an admin needs to find again to unban."""
    await _seed(sqlite_sessionmaker, 3)

    async with sqlite_sessionmaker() as session:
        target = (await UserRepo(session).list_page(offset=0, limit=20))[1]
        target.status = UserStatus.BANNED
        await session.commit()

    async with sqlite_sessionmaker() as session:
        page = await UserRepo(session).list_page(offset=0, limit=20)
        assert len(page) == 3
        assert page[1].status is UserStatus.BANNED


async def test_an_empty_install_lists_nothing_without_crashing(sqlite_sessionmaker) -> None:
    async with sqlite_sessionmaker() as session:
        repo = UserRepo(session)
        assert await repo.count_all() == 0
        assert await repo.list_page(offset=0, limit=20) == []
