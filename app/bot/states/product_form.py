from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ProductForm(StatesGroup):
    category = State()
    name = State()
    description = State()
    price = State()
    fulfillment_mode = State()
    warranty_days = State()
    delivery_info = State()
    # Asked last, and only for AUTO products: a MANUAL product has no stock pool, so prompting for
    # licence keys would be a step with no possible answer. Skippable — the product is then created
    # OUT OF STOCK, which is what every product used to be.
    stock = State()
    confirm = State()


class StockUploadForm(StatesGroup):
    product = State()
    payloads = State()


class ProductImportForm(StatesGroup):
    document = State()


class ProductSearchForm(StatesGroup):
    term = State()


class ProductEditForm(StatesGroup):
    # One state for every free-text field; which field is being edited lives in FSM data, because
    # callback_data is capped at 64 bytes and already carries the product id.
    value = State()
