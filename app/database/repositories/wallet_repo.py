from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.wallet import TxnAccount, TxnStatus, TxnType, Wallet, WalletTransaction


class WalletRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(self, user_id: int, *, currency: str) -> Wallet:
        result = await self._session.execute(select(Wallet).where(Wallet.user_id == user_id))
        wallet = result.scalar_one_or_none()
        if wallet is None:
            wallet = Wallet(user_id=user_id, balance_minor=0, currency=currency)
            self._session.add(wallet)
            await self._session.flush()
        return wallet

    async def get_locked(self, wallet_id: int) -> Wallet:
        """SELECT ... FOR UPDATE — serializes concurrent debits/credits on the same wallet."""
        result = await self._session.execute(select(Wallet).where(Wallet.id == wallet_id).with_for_update())
        wallet = result.scalar_one()
        return wallet

    async def get_transaction_by_idempotency_key(self, key: str) -> WalletTransaction | None:
        result = await self._session.execute(select(WalletTransaction).where(WalletTransaction.idempotency_key == key))
        return result.scalar_one_or_none()

    async def list_pending_topups(self, limit: int = 20) -> list[WalletTransaction]:
        result = await self._session.execute(
            select(WalletTransaction)
            .where(WalletTransaction.type == TxnType.TOPUP, WalletTransaction.status == TxnStatus.PENDING)
            .order_by(WalletTransaction.created_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_main_transactions(
        self, wallet_id: int, *, limit: int = 20, offset: int = 0
    ) -> list[WalletTransaction]:
        """The spendable balance's own ledger — what the buyer sees on 📒 Wallet History.

        Only the MAIN account. The refund side has its own screen, and a row shown on both would be
        counted twice by anyone adding them up. `id` breaks ties on `created_at` because the two
        halves of a refund move are written in the same instant, and without it they can come back
        in either order — so the same page reads differently each time it is opened.
        """
        result = await self._session.execute(
            select(WalletTransaction)
            .where(
                WalletTransaction.wallet_id == wallet_id,
                WalletTransaction.account == TxnAccount.MAIN,
            )
            .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_main_transactions(self, wallet_id: int) -> int:
        result = await self._session.execute(
            select(func.count(WalletTransaction.id)).where(
                WalletTransaction.wallet_id == wallet_id,
                WalletTransaction.account == TxnAccount.MAIN,
            )
        )
        return int(result.scalar_one())

    async def list_refund_transactions(self, wallet_id: int, limit: int = 20) -> list[WalletTransaction]:
        """Only the Refund Wallet side of the ledger — what arrived from declined orders and what an
        admin has since paid out or moved across. Read on the settle screen, where mixing in ordinary
        purchases would bury the three or four rows that actually explain the balance."""
        result = await self._session.execute(
            select(WalletTransaction)
            .where(WalletTransaction.wallet_id == wallet_id, WalletTransaction.account == TxnAccount.REFUND)
            .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_wallets_with_refunds(self, limit: int = 50) -> list[Wallet]:
        """Every wallet currently holding refund money, largest first.

        Largest rather than newest on purpose: this is a list of debts, and the biggest one is the one
        a buyer is most likely to be chasing.
        """
        result = await self._session.execute(
            select(Wallet)
            .where(Wallet.refund_balance_minor > 0)
            .order_by(Wallet.refund_balance_minor.desc(), Wallet.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def total_refund_held(self) -> int:
        result = await self._session.execute(select(func.coalesce(func.sum(Wallet.refund_balance_minor), 0)))
        return int(result.scalar_one())
