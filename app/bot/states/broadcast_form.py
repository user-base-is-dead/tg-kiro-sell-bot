from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class BroadcastForm(StatesGroup):
    """Two steps only. There is deliberately no title step: the internal title is derived from the
    first line of the body, so tapping Broadcast drops the admin straight into writing."""

    body = State()  # collecting one or more messages
    confirm = State()  # preview shown; waiting on Send or Back
