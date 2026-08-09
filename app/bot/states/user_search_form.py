from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class UserSearchForm(StatesGroup):
    query = State()


class UserBalanceForm(StatesGroup):
    # Which account is being adjusted lives in FSM data, not in the callback, so the typed amount
    # never has to compete for the 64-byte callback_data budget.
    amount = State()
