from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import new_idempotency_key
from app.database.models.wallet import TxnAccount, TxnStatus, TxnType, WalletTransaction
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


async def admin_adjust(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    currency: str,
    reason: str,
) -> WalletTransaction:
    """A signed manual adjustment: positive credits, negative debits. One place for it so the
    /adjust_balance command and the button on a user's profile cannot drift apart — they must write
    the same transaction type and the same audit-visible ref, or the ledger stops being readable.

    Raises UserError when a debit would take the balance below zero.
    """
    if amount_minor == 0:
        raise UserError("Amount can't be zero.")

    key = new_idempotency_key()
    mover = credit if amount_minor > 0 else debit
    return await mover(
        session,
        user_id=user_id,
        amount_minor=abs(amount_minor),
        currency=currency,
        type_=TxnType.ADMIN_ADJUST,
        idempotency_key=key,
        ref_type="admin_adjust",
        ref_id=reason[:64],
    )


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


async def credit_refund_balance(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    currency: str,
    idempotency_key: str,
    type_: TxnType = TxnType.REFUND_PARK,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> WalletTransaction:
    """Park money in the Refund Wallet — the money side of a declined order.

    Deliberately NOT `credit()`. The spendable balance is what `debit()` charges, so crediting it
    would turn money owed back on a cancelled order into credit for the next purchase, silently and
    irreversibly. This balance can only leave by an admin recording a payout or moving it across, both
    of which are decisions a person makes.
    """
    repo = WalletRepo(session)
    existing = await repo.get_transaction_by_idempotency_key(idempotency_key)
    if existing is not None:
        return existing

    wallet = await repo.get_or_create(user_id, currency=currency)
    wallet = await repo.get_locked(wallet.id)

    wallet.refund_balance_minor += amount_minor
    wallet.version += 1

    txn = WalletTransaction(
        wallet_id=wallet.id,
        type=type_,
        account=TxnAccount.REFUND,
        amount_minor=amount_minor,
        balance_after_minor=wallet.refund_balance_minor,
        status=TxnStatus.COMPLETED,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
    )
    session.add(txn)
    await session.flush()
    return txn


async def debit_refund_balance(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    currency: str,
    idempotency_key: str,
    type_: TxnType = TxnType.REFUND_PAYOUT,
    note: str | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> WalletTransaction:
    """Take money out of the Refund Wallet — an admin recording what they actually sent on chain.

    Raises UserError('errors.refund_balance_short') without touching anything if the balance can't
    cover it, so a mistyped payout rolls the caller's transaction back cleanly instead of driving a
    balance negative.
    """
    repo = WalletRepo(session)
    existing = await repo.get_transaction_by_idempotency_key(idempotency_key)
    if existing is not None:
        return existing

    wallet = await repo.get_or_create(user_id, currency=currency)
    wallet = await repo.get_locked(wallet.id)

    if wallet.refund_balance_minor < amount_minor:
        raise UserError("errors.refund_balance_short")

    wallet.refund_balance_minor -= amount_minor
    wallet.version += 1

    txn = WalletTransaction(
        wallet_id=wallet.id,
        type=type_,
        account=TxnAccount.REFUND,
        amount_minor=-amount_minor,
        balance_after_minor=wallet.refund_balance_minor,
        status=TxnStatus.COMPLETED,
        ref_type=ref_type,
        ref_id=ref_id,
        admin_note=note,
        idempotency_key=idempotency_key,
    )
    session.add(txn)
    await session.flush()
    return txn


async def move_refund_to_spendable(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    currency: str,
    note: str | None = None,
) -> tuple[WalletTransaction, WalletTransaction]:
    """Refund Wallet -> spendable balance, as two ledger rows.

    Two rows rather than one moved number, because the ledger is what makes both balances checkable:
    sum the MAIN rows and you must get `balance_minor`, sum the REFUND rows and you must get
    `refund_balance_minor`. A single row would leave one of those sums permanently wrong.

    Returns (refund_side, main_side). Raises UserError if the refund balance is short.
    """
    key = new_idempotency_key()
    out = await debit_refund_balance(
        session,
        user_id=user_id,
        amount_minor=amount_minor,
        currency=currency,
        idempotency_key=f"refmove-out:{key}",
        type_=TxnType.REFUND_MOVE,
        note=note,
        ref_type="refund_move",
    )
    into = await credit(
        session,
        user_id=user_id,
        amount_minor=amount_minor,
        currency=currency,
        type_=TxnType.REFUND_MOVE,
        idempotency_key=f"refmove-in:{key}",
        ref_type="refund_move",
    )
    return out, into


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
    wallet can't cover it — the caller's transaction rolls back cleanly.

    Only ever touches the spendable balance. The Refund Wallet is unreachable from here by design —
    see `credit_refund_balance`.
    """
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
