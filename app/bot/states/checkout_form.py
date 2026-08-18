from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class CheckoutForm(StatesGroup):
    """Buy Now asks how many before it asks how to pay.

    The number is typed rather than picked from a row of buttons: stock runs to whatever the admin
    uploaded, and a keyboard cannot offer 37 without becoming a wall of digits.
    """

    quantity = State()
