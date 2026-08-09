from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class CategoryForm(StatesGroup):
    name = State()
    emoji = State()
    description = State()
    confirm = State()
