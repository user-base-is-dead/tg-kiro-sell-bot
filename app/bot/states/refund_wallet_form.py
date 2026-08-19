from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class RefundPayoutForm(StatesGroup):
    """Recording what was actually sent out of band — amount plus a note naming the transfer."""

    amount = State()


class RefundMoveForm(StatesGroup):
    """How much of a parked refund becomes ordinary spendable balance."""

    amount = State()
