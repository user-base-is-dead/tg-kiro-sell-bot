from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class GiftRedeemForm(StatesGroup):
    code = State()


class GiftCreateForm(StatesGroup):
    value = State()
    max_uses = State()
    per_user_limit = State()
    expires_days = State()
    description = State()
