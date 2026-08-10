from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

# One place for every CallbackData factory, so prefix collisions are caught at review time
# rather than at runtime. Packed strings must stay <= 64 bytes (Telegram's callback_data limit) —
# anything that would exceed it goes through InteractionState instead (see app/utils/state.py,
# added in Phase 2 once payloads get big enough to need it).


class NavCB(CallbackData, prefix="nav"):
    """Generic navigation: home, and back-to-<target> where target is itself a short nav token."""

    target: str


class LangCB(CallbackData, prefix="lang"):
    locale: str


class CategoryCB(CallbackData, prefix="cat"):
    action: str  # "open" | "page"
    id: str = ""
    page: int = 1


class ProductCB(CallbackData, prefix="prod"):
    action: str  # "view" | "buy"
    id: str
    cat_id: str = ""
    page: int = 1


class AdminCategoryCB(CallbackData, prefix="acat"):
    action: str  # "list" | "add" | "edit" | "delete" | "toggle" | "view"
    id: str = ""


class AdminProductCB(CallbackData, prefix="aprod"):
    action: str  # "list" | "add" | "edit" | "delete" | "toggle" | "view" | "stock"
    id: str = ""
    page: int = 1


class OrderCB(CallbackData, prefix="ord"):
    action: str  # "pay" | "wallet" | "crypto" | "confirm" | "cancel" | "view"
    product_id: str = ""
    order_id: str = ""
    page: int = 1


class AdminOrderCB(CallbackData, prefix="aord"):
    action: str  # "list" | "view" | "fulfill" | "cancel"
    id: str = ""
    page: int = 1


class AdminPaymentCB(CallbackData, prefix="apay"):
    action: str  # "list" | "approve" | "reject"
    id: str = ""


class AdminGiftCB(CallbackData, prefix="agift"):
    action: str  # "list" | "add" | "view" | "disable"
    id: str = ""


class SupportCB(CallbackData, prefix="sup"):
    action: str  # "create" | "mytickets" | "view" | "close"
    id: str = ""
    category: str = ""


class AdminTicketCB(CallbackData, prefix="atick"):
    action: str  # "list" | "close"
    id: str = ""


class AdminMiscCB(CallbackData, prefix="amisc"):
    action: str  # "dashboard" | "settings" | "logs" | "broadcast" | "users"
    id: str = ""
    page: int = 1


class AdminUserCB(CallbackData, prefix="auser"):
    action: str  # "list" | "view" | "ban" | "unban" | "credit" | "search"
    id: str = ""
    page: int = 1
