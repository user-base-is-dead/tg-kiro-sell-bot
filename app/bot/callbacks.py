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
    # "delete" only asks for confirmation; "delete_ok" is the one that actually removes the row.
    # "cat" opens one category folder in the admin list; `id` is the category, not a product.
    # "dup" flips `allow_duplicate_stock` — whether this product may hold several copies of one
    # login, which is what a shared account needs.
    action: str  # "list" | "cat" | "add" | "edit" | "delete" | "delete_ok" | "toggle" | "dup" | "view" | "stock"
    id: str = ""
    page: int = 1


class AdminStockCB(CallbackData, prefix="astk"):
    """One product's individual stock items (📋 Stock items), and the Add Stock split question.

    "list" pages through a product's items — `view` "u" is in stock, "c" in checkout (being paid
    for), "s" sold; "item" opens one;
    "edit" asks for new content; "del" asks to confirm a removal and "del_ok" does it.
    "split" / "wsplit" answer "how should this pasted message be added?" in Add Stock and in the
    new-product wizard: `id` is the admin's message it refers to, `view` the answer (see
    stock_service: "l" one per line, "b" per blank-line block, "k" keep whole, "x" discard).
    """

    action: str
    pid: str = ""  # product id
    id: str = ""  # stock item id, or the pasted message's id for split/wsplit
    view: str = "u"
    page: int = 1


class OrderCB(CallbackData, prefix="ord"):
    action: str  # "pay" | "wallet" | "crypto" | "confirm" | "cancel" | "view"
    product_id: str = ""
    order_id: str = ""
    page: int = 1
    # How many units the buyer asked for. Carried through every checkout screen rather than left in
    # FSM state: the buyer can leave the payment screen open, wander off and come back, and a
    # button that has forgotten the number would quietly charge them for one.
    qty: int = 1


class AdminOrderCB(CallbackData, prefix="aord"):
    # "cancel" is kept alongside "decline" so buttons already sitting in an admin's chat history from
    # before the reason prompt existed still land somewhere sensible instead of doing nothing.
    action: str  # "list" | "all" | "view" | "fulfill" | "decline" | "cancel" | "search"
    id: str = ""
    page: int = 1


class AdminRefundCB(CallbackData, prefix="aref"):
    # "list" is the queue of everyone owed money; "view" is one buyer's settle screen; "payout"
    # records what was sent on chain; "move" turns parked money into spendable balance; "unfreeze"
    # releases the whole Frozen Wallet into the Refund Wallet; "sanction" blocks a typed amount of the
    # Refund Wallet and "release" lifts it again.
    # No "moveall": it was the same action as "move" with the amount decided for you, and having both
    # made one decision look like two. Both prompts take a typed amount now.
    # Keep action names short: the packed data carries a 36-character order id and must fit Telegram's
    # 64-byte limit.
    action: str  # "list" | "view" | "payout" | "move" | "unfreeze" | "sanction" | "release"
    id: str = ""  # user id
    order_id: str = ""
    # Where the settle screen was opened FROM, so its Back button returns there instead of always
    # dumping the admin on the refund queue — which, reached from a user's profile, is a list that
    # never contained the screen they were just on.
    src: str = "list"  # "list" | "profile" | "order"
    page: int = 1


class AdminPaymentCB(CallbackData, prefix="apay"):
    action: str  # "list" | "approve" | "reject"
    id: str = ""


class SupportCB(CallbackData, prefix="sup"):
    action: str  # "create" | "mytickets" | "view" | "close"
    id: str = ""
    category: str = ""


class AdminTicketCB(CallbackData, prefix="atick"):
    action: str  # "list" | "close"
    id: str = ""


class AdminMiscCB(CallbackData, prefix="amisc"):
    action: str  # "dashboard" | "settings" | "logs" | "broadcast" | "users" | "refunds"
    id: str = ""
    page: int = 1


class AdminUserCB(CallbackData, prefix="auser"):
    action: str  # "list" | "view" | "ban" | "unban" | "credit" | "search"
    id: str = ""
    page: int = 1
