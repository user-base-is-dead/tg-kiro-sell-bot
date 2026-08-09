from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class SettingsForm(StatesGroup):
    referral_reward = State()
