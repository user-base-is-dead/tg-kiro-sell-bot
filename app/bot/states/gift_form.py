from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class GiftRedeemForm(StatesGroup):
    code = State()


class GiftCreateForm(StatesGroup):
    kind = State()  # wallet credit or a product
    value = State()  # CREDIT branch only
    # The gift carries its own items, pasted here. It deliberately cannot point at a catalog
    # product — a giveaway must not draw down stock that paying customers are queueing for.
    items = State()  # ITEM branch only
    max_uses = State()
    per_user_limit = State()
    expires_days = State()
    description = State()
    review = State()  # everything collected; waiting on Confirm


class GiftEditForm(StatesGroup):
    """Editing a code that may already be in circulation — description, limits, expiry. What the
    code *grants* is not editable: changing that under a holder is a different code, not an edit."""

    value = State()


class GiftAddItemsForm(StatesGroup):
    """Topping an ITEM code up, so the code people already hold stretches further."""

    payloads = State()


class GiftItemEditForm(StatesGroup):
    """Rewriting one unclaimed item of an ITEM code — a typo'd key fixed in place, so the code
    people already hold does not have to be reissued."""

    payload = State()
