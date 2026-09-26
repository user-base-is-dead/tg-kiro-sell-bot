from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class RefundPayoutForm(StatesGroup):
    """Recording what was actually sent out of band — amount plus a note naming the transfer."""

    amount = State()


class RefundMoveForm(StatesGroup):
    """How much of a parked refund becomes ordinary spendable balance."""

    amount = State()


class RefundSanctionForm(StatesGroup):
    """How much of a parked refund to sanction (block), plus an optional reason the buyer is shown."""

    amount = State()


class RefundReleaseForm(StatesGroup):
    """How much of a sanction to lift, back into the Refund Wallet."""

    amount = State()
