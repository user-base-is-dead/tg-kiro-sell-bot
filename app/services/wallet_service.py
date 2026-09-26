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


async def credit_frozen_balance(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    currency: str,
    idempotency_key: str,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> WalletTransaction:
    """Freeze money — the other place a declined order's money can go, instead of the Refund Wallet.

    Frozen money is still the buyer's and they can see it, but it is out of reach of everything:
    `debit()` only charges the spendable balance, and payouts and moves only draw on the refund
    balance. The one way out is `release_frozen_to_refund`, which an admin triggers.

    Callers pass the same idempotency key they would have used to park it for refund
    (`refund:<order.id>`), so a given order's money can land in exactly one of the two wallets, once.
    """
    repo = WalletRepo(session)
    existing = await repo.get_transaction_by_idempotency_key(idempotency_key)
    if existing is not None:
        return existing

    wallet = await repo.get_or_create(user_id, currency=currency)
    wallet = await repo.get_locked(wallet.id)

    wallet.frozen_balance_minor += amount_minor
    wallet.version += 1

    txn = WalletTransaction(
        wallet_id=wallet.id,
        type=TxnType.FROZEN_PARK,
        account=TxnAccount.FROZEN,
        amount_minor=amount_minor,
        balance_after_minor=wallet.frozen_balance_minor,
        status=TxnStatus.COMPLETED,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
    )
    session.add(txn)
    await session.flush()
    return txn


async def release_frozen_to_refund(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    currency: str,
    note: str | None = None,
) -> tuple[WalletTransaction, WalletTransaction]:
    """Frozen Wallet -> Refund Wallet, as two ledger rows — one per account, for the same reason
    `move_refund_to_spendable` writes two: every account's rows must still sum to its balance.

    Returns (frozen_side, refund_side). Raises UserError without touching anything if the frozen
    balance can't cover it.
    """
    if amount_minor <= 0:
        raise UserError("errors.invalid_amount")

    repo = WalletRepo(session)
    wallet = await repo.get_or_create(user_id, currency=currency)
    wallet = await repo.get_locked(wallet.id)

    if wallet.frozen_balance_minor < amount_minor:
        raise UserError("errors.frozen_balance_short")

    key = new_idempotency_key()
    wallet.frozen_balance_minor -= amount_minor
    out = WalletTransaction(
        wallet_id=wallet.id,
        type=TxnType.FROZEN_RELEASE,
        account=TxnAccount.FROZEN,
        amount_minor=-amount_minor,
        balance_after_minor=wallet.frozen_balance_minor,
        status=TxnStatus.COMPLETED,
        ref_type="frozen_release",
        admin_note=note,
        idempotency_key=f"frzrel-out:{key}",
    )
    wallet.refund_balance_minor += amount_minor
    wallet.version += 1
    into = WalletTransaction(
        wallet_id=wallet.id,
        type=TxnType.FROZEN_RELEASE,
        account=TxnAccount.REFUND,
        amount_minor=amount_minor,
        balance_after_minor=wallet.refund_balance_minor,
        status=TxnStatus.COMPLETED,
        ref_type="frozen_release",
        idempotency_key=f"frzrel-in:{key}",
    )
    session.add_all([out, into])
    await session.flush()
    return out, into


async def _shift(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    currency: str,
    source: TxnAccount,
    target: TxnAccount,
    type_: TxnType,
    short_error: str,
    key_prefix: str,
    note: str | None,
) -> tuple[WalletTransaction, WalletTransaction]:
    """Move money between two of a wallet's non-spendable balances, as one ledger row per side.

    The shared body of sanctioning and releasing: take `amount_minor` off `source`, put it on
    `target`, and write both halves so each account's rows still sum to its balance. Raises
    UserError(`short_error`) without touching anything if `source` can't cover it.
    """
    if amount_minor <= 0:
        raise UserError("errors.invalid_amount")

    repo = WalletRepo(session)
    wallet = await repo.get_or_create(user_id, currency=currency)
    wallet = await repo.get_locked(wallet.id)

    column = {
        TxnAccount.REFUND: "refund_balance_minor",
        TxnAccount.SANCTIONED: "sanctioned_balance_minor",
    }
    if getattr(wallet, column[source]) < amount_minor:
        raise UserError(short_error)

    key = new_idempotency_key()
    setattr(wallet, column[source], getattr(wallet, column[source]) - amount_minor)
    out = WalletTransaction(
        wallet_id=wallet.id,
        type=type_,
        account=source,
        amount_minor=-amount_minor,
        balance_after_minor=getattr(wallet, column[source]),
        status=TxnStatus.COMPLETED,
        ref_type=type_.value.lower(),
        admin_note=note,
        idempotency_key=f"{key_prefix}-out:{key}",
    )
    setattr(wallet, column[target], getattr(wallet, column[target]) + amount_minor)
    wallet.version += 1
    into = WalletTransaction(
        wallet_id=wallet.id,
        type=type_,
        account=target,
        amount_minor=amount_minor,
        balance_after_minor=getattr(wallet, column[target]),
        status=TxnStatus.COMPLETED,
        ref_type=type_.value.lower(),
        admin_note=note,
        idempotency_key=f"{key_prefix}-in:{key}",
    )
    session.add_all([out, into])
    await session.flush()
    return out, into


async def sanction_refund_balance(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    currency: str,
    note: str | None = None,
) -> tuple[WalletTransaction, WalletTransaction]:
    """Refund Wallet -> Sanctioned: block part or all of what a buyer is owed.

    Any amount, at any time after the money was parked — unlike freezing, which happens at the
    decline and takes the whole order. Sanctioned money stays the buyer's and on their screen, but
    payouts and moves only draw on the refund balance and `debit()` only on the spendable one, so
    nothing can spend it or send it. `release_sanctioned_to_refund` is the one way back.

    Returns (refund_side, sanctioned_side). Raises UserError if the refund balance is short.
    """
    return await _shift(
        session,
        user_id=user_id,
        amount_minor=amount_minor,
        currency=currency,
        source=TxnAccount.REFUND,
        target=TxnAccount.SANCTIONED,
        type_=TxnType.SANCTION,
        short_error="errors.refund_balance_short",
        key_prefix="snc",
        note=note,
    )


async def release_sanctioned_to_refund(
    session: AsyncSession,
    *,
    user_id: int,
    amount_minor: int,
    currency: str,
    note: str | None = None,
) -> tuple[WalletTransaction, WalletTransaction]:
    """Sanctioned -> Refund Wallet: lift a sanction, in part or in full.

    Returns (sanctioned_side, refund_side). Raises UserError if the sanctioned balance is short.
    """
    return await _shift(
        session,
        user_id=user_id,
        amount_minor=amount_minor,
        currency=currency,
        source=TxnAccount.SANCTIONED,
        target=TxnAccount.REFUND,
        type_=TxnType.SANCTION_RELEASE,
        short_error="errors.sanctioned_balance_short",
        key_prefix="srl",
        note=note,
    )


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
