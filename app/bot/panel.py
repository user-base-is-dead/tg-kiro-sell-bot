from __future__ import annotations

import logging

from aiogram.types import ReplyKeyboardRemove

logger = logging.getLogger(__name__)

# Telegram ids this process has already sent the removal to. In-memory on purpose: a restart
# re-sends it, which is exactly what you want if a client somehow held on to the old panel.
_cleared: set[int] = set()


def reset_installed_panels() -> None:
    """Test hook. Production never needs this: a bot restart clears the set by itself."""
    _cleared.clear()


def panel_markup(
    telegram_id: int, locale: str, *, is_admin: bool, force: bool = False
) -> ReplyKeyboardRemove | None:
    """There is no bottom panel any more. This now *removes* one, and returns `None` once the user
    is known to be clear of it.

    The panel used to be a persistent ReplyKeyboardMarkup mirroring the inline main menu. It was
    duplicate navigation, and `is_persistent=True` made it genuinely unremovable from the user's
    side — Telegram keeps a persistent reply keyboard pinned to the input area until the *bot*
    sends a `ReplyKeyboardRemove`. Closing it in the client only hides it until the next message.
    So this is the only thing that takes it down, and it has to ride on a real message.

    Callers are unchanged: they still attach whatever this returns to a message they were sending
    anyway. `force` (used by `/start`) re-sends the removal even if this process already did,
    which is the recovery path for a client that still shows a stale panel.

    `locale` and `is_admin` are kept in the signature because callers pass them; nothing about a
    removal varies by either.
    """
    if telegram_id in _cleared and not force:
        return None

    _cleared.add(telegram_id)
    return ReplyKeyboardRemove()
