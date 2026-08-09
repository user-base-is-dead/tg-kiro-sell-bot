from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class BroadcastForm(StatesGroup):
    title = State()
    body = State()
    confirm = State()
