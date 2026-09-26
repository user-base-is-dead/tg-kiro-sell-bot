"""The first message in every staff topic: which commands exist there and what each one does.

New support staff land in groups full of topics the bot opened, and nothing in them said how to
answer a buyer, how to close a ticket, or what /done means — the commands were only discoverable by
reading the code. So every topic the bot opens now starts with a guide, tailored to what the topic
is: a plain support ticket, a warranty claim, or an order's log.

Kept in one place so the three guides cannot drift apart, and so a new command is documented where
staff will actually see it.
"""

from __future__ import annotations

from app.services.warranty_service import CLAIM_GRACE

_REPLY = (
    "<b>Reply to the buyer</b>\n"
    "Just type in this topic — your text reaches the buyer's DM as “🎧 PowerX Support”, and photos, "
    "videos, files and voice notes are passed on too. Anything that starts with / is never sent to "
    "the buyer."
)

# Group membership is the staff list (app/bot/filters/staff.py), so a new team member needs to be
# told they can act, not just how.
_ANYONE = "👥 Everyone in this group can do all of the above — no admin ID needed."

# Collapsed by default, so the commands that matter in this topic are what staff see first.
_MORE = (
    "<blockquote expandable><b>Admin-only commands</b> — for IDs in ADMIN_IDS, best used in the "
    "bot's private chat\n"
    "<code>/admin</code> — admin panel\n"
    "<code>/open_tickets</code> — every open support ticket\n"
    "<code>/pending_orders</code> — orders waiting to be fulfilled\n"
    "<code>/refund_wallets</code> — refunds waiting to be settled\n"
    "<code>/wallet_balances</code> — who is holding store credit\n"
    "<code>/adjust_balance ID +10.00 reason</code> — add to or take from a buyer's wallet\n"
    "<code>/dashboard</code> — sales and user stats\n"
    "<code>/broadcast_status</code> — how far a broadcast has got</blockquote>"
)


def ticket_guide() -> str:
    """For an ordinary support ticket's topic."""
    return (
        "📋 <b>Staff guide · Support ticket</b>\n\n"
        f"{_REPLY}\n\n"
        "<b>Commands in this topic</b>\n"
        "<code>/close</code> — close the ticket. The buyer is told, and this topic turns read-only.\n\n"
        f"{_ANYONE}\n\n"
        f"{_MORE}"
    )


def warranty_claim_guide() -> str:
    """For a warranty claim's topic: the claim has three endings of its own, on top of chatting."""
    hours = int(CLAIM_GRACE.total_seconds() // 3600)
    return (
        "📋 <b>Staff guide · Warranty claim</b>\n\n"
        f"{_REPLY}\n\n"
        "<b>Settle the claim</b> — the warranty ID is in the claim message below\n"
        "<code>/done ID</code> — item replaced: the buyer keeps the warranty time that was left, and "
        "this topic closes.\n"
        "<code>/refund ID reason</code> — pay them back instead: the money goes to their Refund "
        "Wallet, the warranty ends, and this topic stays open until the money is settled.\n"
        "<code>/reject ID reason</code> — turn the claim down: the original warranty carries on, and "
        "this topic closes.\n"
        f"If nobody answers within {hours} hours, the claim closes by itself.\n\n"
        "<b>Commands in this topic</b>\n"
        "<code>/close</code> — close the topic once a refunded claim's money is settled.\n\n"
        f"{_ANYONE}\n\n"
        f"{_MORE}"
    )


def order_guide() -> str:
    """For an order's topic in the orders group — a log for staff, never a chat with the buyer."""
    return (
        "📋 <b>Staff guide · Order thread</b>\n\n"
        "This topic is the order's log: the card below always shows where the order stands, and "
        "every step is posted under it. It is for staff only — nothing typed here reaches the buyer.\n\n"
        "<b>Manual orders</b> — use the buttons on the “awaiting fulfilment” message\n"
        "<b>✅ Fulfill now</b> — then send the delivery content as your next message here. The buyer "
        "gets it exactly as you format it.\n"
        "<b>🚫 Decline</b> — then send the reason and pick where their money goes: ↩️ Refund Wallet "
        "(settled with them later) or 🧊 Frozen Wallet (on hold until you unfreeze it). The buyer "
        "reads the reason word for word.\n"
        "<code>/cancel</code> — stop a fulfil or decline you started.\n\n"
        "<b>Commands in this topic</b>\n"
        "<code>/close</code> — close this thread. Delivered orders close by themselves; a declined "
        "order's thread stays open while its refund is parked — close it once you're done.\n\n"
        f"{_ANYONE} Paying out, moving or unfreezing the money afterwards (💸 Refund Wallets) is "
        "for admins.\n\n"
        f"{_MORE}"
    )
