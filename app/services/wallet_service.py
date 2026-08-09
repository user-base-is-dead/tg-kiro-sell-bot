from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import new_idempotency_key
from app.database.models.wallet import TxnStatus, TxnType, WalletTransaction
from app.database.repositories.wallet_repo import WalletRepo
from app.utils.errors import UserError


async def credit(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    currency: str,
    type_: TxnType,
    idempotency_key: str,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> WalletTransaction:
    existing = await WalletRepo(session).get_transaction_by_idempotency_key(idempotency_key)
    if existing is not None:
        return existing

    repo = WalletRepo(session)
    wallet = await repo.get_or_create(user_id, currency=currency)
    wallet = await repo.get_locked(wallet.id)

    wallet.balance_minor += amount_minor
    wallet.version += 1

    txn = WalletTransaction(
        wallet_id=wallet.id,
        type=type_,
        amount_minor=amount_minor,
        balance_after_minor=wallet.balance_minor,
        status=TxnStatus.COMPLETED,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
    )
    session.add(txn)
    await session.flush()
    return txn


async def create_pending_topup(
    session: AsyncSession, *, user_id: int, amount_minor: int, currency: str, proof: str
) -> WalletTransaction:
    """Doesn't touch the balance — that only happens on admin approval. balance_after_minor
    is left as the *current* balance until then, so an unapproved request never inflates it."""
    repo = WalletRepo(session)
    wallet = await repo.get_or_create(user_id, currency=currency)

    txn = WalletTransaction(
        wallet_id=wallet.id,
        type=TxnType.TOPUP,
        amount_minor=amount_minor,
        balance_after_minor=wallet.balance_minor,
        status=TxnStatus.PENDING,
        proof=proof,
        idempotency_key=new_idempotency_key(),
    )
    session.add(txn)
    await session.flush()
    return txn


async def approve_topup(session: AsyncSession, *, transaction_id: int, admin_telegram_id: int) -> WalletTransaction:
    repo = WalletRepo(session)
    txn = await session.get(WalletTransaction, transaction_id)
    if txn is None or txn.status != TxnStatus.PENDING or txn.type != TxnType.TOPUP:
        raise UserError("common.unknown_action")

    wallet = await repo.get_locked(txn.wallet_id)
    wallet.balance_minor += txn.amount_minor
    wallet.version += 1

    txn.status = TxnStatus.COMPLETED
    txn.balance_after_minor = wallet.balance_minor
    txn.reviewed_by_admin_id = admin_telegram_id
    await session.flush()
    return txn


async def reject_topup(
    session: AsyncSession, *, transaction_id: int, admin_telegram_id: int, reason: str
) -> WalletTransaction:
    txn = await session.get(WalletTransaction, transaction_id)
    if txn is None or txn.status != TxnStatus.PENDING or txn.type != TxnType.TOPUP:
        raise UserError("common.unknown_action")

    txn.status = TxnStatus.FAILED
    txn.admin_note = reason
    txn.reviewed_by_admin_id = admin_telegram_id
    await session.flush()
    return txn


async def debit(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    currency: str,
    type_: TxnType,
    idempotency_key: str,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> WalletTransaction:
    """Raises UserError('errors.insufficient_balance') without mutating anything if the
    wallet can't cover it — the caller's transaction rolls back cleanly."""
    existing = await WalletRepo(session).get_transaction_by_idempotency_key(idempotency_key)
    if existing is not None:
        return existing

    repo = WalletRepo(session)
    wallet = await repo.get_or_create(user_id, currency=currency)
    wallet = await repo.get_locked(wallet.id)

    if wallet.balance_minor < amount_minor:
        raise UserError("errors.insufficient_balance")

    wallet.balance_minor -= amount_minor
    wallet.version += 1

    txn = WalletTransaction(
        wallet_id=wallet.id,
        type=type_,
        amount_minor=-amount_minor,
        balance_after_minor=wallet.balance_minor,
        status=TxnStatus.COMPLETED,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
    )
    session.add(txn)
    await session.flush()
    return txn
