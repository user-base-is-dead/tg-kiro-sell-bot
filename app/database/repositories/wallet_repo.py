from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.wallet import TxnStatus, TxnType, Wallet, WalletTransaction


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

    async def list_transactions(self, wallet_id: int, limit: int = 20) -> list[WalletTransaction]:
        result = await self._session.execute(
            select(WalletTransaction)
            .where(WalletTransaction.wallet_id == wallet_id)
            .order_by(WalletTransaction.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
