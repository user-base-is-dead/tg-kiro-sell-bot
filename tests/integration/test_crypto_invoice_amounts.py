from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.bot.handlers.payments.topup_crypto import PAYMENT_TIMEOUT_MINUTES, render_payment_details
from app.database.models.crypto import CryptoPayment
from app.database.repositories.user_repo import UserRepo


async def _user(session, telegram_id: int) -> int:
    user, _ = await UserRepo(session).upsert_from_telegram(
        telegram_id=telegram_id, username=f"u{telegram_id}", first_name="T", last_name=None,
        chat_id=telegram_id, default_locale="en",
    )
    await session.flush()
    return user.id


async def test_two_live_invoices_for_the_same_amount_get_different_totals(sqlite_sessionmaker) -> None:
    """Matching is by amount alone, so two buyers topping up $15.00 at once produced two invoices
    expecting the identical total. The checker calls that ambiguous and skips the transfer — both
    buyers pay and neither is credited. Every live invoice therefore gets its own total."""
    async with sqlite_sessionmaker() as session:
        a = await _user(session, 7001)
        b = await _user(session, 7002)

        await render_payment_details(session, a, 15.0, "en")
        await render_payment_details(session, b, 15.0, "en")

        totals = [float(p.expected_amount) for p in (await session.execute(select(CryptoPayment))).scalars()]

    assert len(totals) == 2
    assert totals[0] != totals[1]


async def test_credited_amount_is_unaffected_by_the_disambiguating_cents(sqlite_sessionmaker) -> None:
    """The nudge lives in the fee, never in what the buyer gets. product_amount_minor is what the
    wallet is credited with, and it must stay exactly the amount that was asked for."""
    async with sqlite_sessionmaker() as session:
        a = await _user(session, 7003)
        b = await _user(session, 7004)

        await render_payment_details(session, a, 15.0, "en")
        await render_payment_details(session, b, 15.0, "en")

        payments = list((await session.execute(select(CryptoPayment))).scalars())

    assert [p.product_amount_minor for p in payments] == [1500, 1500]
    # The buyer never pays less than the quoted amount plus the base fee.
    assert all(float(p.expected_amount) >= 15.20 for p in payments)


async def test_settled_and_expired_invoices_do_not_reserve_amounts(sqlite_sessionmaker) -> None:
    """Only PENDING invoices inside their window can be confused with each other. If dead ones
    kept holding their totals, the fee would creep upward forever as the bot aged."""
    async with sqlite_sessionmaker() as session:
        a = await _user(session, 7005)
        b = await _user(session, 7006)

        await render_payment_details(session, a, 15.0, "en")
        first = (await session.execute(select(CryptoPayment))).scalars().one()
        first.status = "CANCELLED"
        await session.flush()

        await render_payment_details(session, b, 15.0, "en")
        second = (
            await session.execute(select(CryptoPayment).where(CryptoPayment.id != first.id))
        ).scalars().one()

    assert float(second.expected_amount) == float(first.expected_amount)


async def test_expired_pending_invoice_releases_its_amount(sqlite_sessionmaker) -> None:
    """An invoice past its 15-minute window is no longer matchable even though its row still says
    PENDING, so it must not push the next buyer's total up."""
    async with sqlite_sessionmaker() as session:
        a = await _user(session, 7007)
        b = await _user(session, 7008)

        await render_payment_details(session, a, 15.0, "en")
        first = (await session.execute(select(CryptoPayment))).scalars().one()
        first.created_at = datetime.now(UTC) - timedelta(minutes=PAYMENT_TIMEOUT_MINUTES + 1)
        await session.flush()

        await render_payment_details(session, b, 15.0, "en")
        second = (
            await session.execute(select(CryptoPayment).where(CryptoPayment.id != first.id))
        ).scalars().one()

    assert float(second.expected_amount) == float(first.expected_amount)
