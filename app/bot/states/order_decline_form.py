from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class OrderDeclineForm(StatesGroup):
    # Which order is being declined lives in FSM data, not in the callback, so the typed reason never
    # has to compete for the 64-byte callback_data budget.
    reason = State()


class OrderSearchForm(StatesGroup):
    term = State()
