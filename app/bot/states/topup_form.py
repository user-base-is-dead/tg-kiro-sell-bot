from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class TopUpForm(StatesGroup):
    amount = State()
    proof = State()
