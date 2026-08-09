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
    confirm = State()


class StockUploadForm(StatesGroup):
    product = State()
    payloads = State()
