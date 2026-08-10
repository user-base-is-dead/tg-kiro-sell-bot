from __future__ import annotations

import logging

from aiogram.types import Message

from app.bot.keyboards.main_menu import main_reply_keyboard
from app.locales.i18n import t

logger = logging.getLogger(__name__)

# Telegram identities (telegram_id, locale, is_admin) whose panel this process has already
# installed. Deliberately in-memory and deliberately NOT persisted — see `send_reply_panel`.
_installed: set[tuple[int, str, bool]] = set()


def reset_installed_panels() -> None:
    """Test hook. Production never needs this: a bot restart clears the set by itself."""
    _installed.clear()


def is_sendable_text(text: str | None) -> bool:
    """True if `text` has real, visible content once whitespace is stripped.

    Guards against sending an empty string, a whitespace-only string, or a string made only of
    zero-width/invisible Unicode (e.g. U+2060 WORD JOINER, U+200B ZERO WIDTH SPACE) — Telegram
    either rejects those outright or delivers them as a bubble with nothing readable in it.
    `str.strip()` only trims leading/trailing whitespace, so this also checks that at least one
    remaining character is neither whitespace nor zero-width, wherever it falls in the string.
    """
    if text is None:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    zero_width = {"​", "‌", "‍", "⁠", "﻿"}
    return any(not ch.isspace() and ch not in zero_width for ch in stripped)


async def send_reply_panel(message: Message, locale: str, *, is_admin: bool, telegram_id: int) -> None:
    """Install the bottom panel without leaving a bubble in the chat.

    READ THIS BEFORE CHANGING ANYTHING HERE. Every "obvious" alternative has been tried on the real
    bot and failed. The Bot API constraints, all three verified in production:

      1. one `sendMessage` cannot carry an inline keyboard and a reply keyboard together;
      2. `editMessageText`/`editMessageReplyMarkup` accept only an *inline* keyboard — a message
         that was sent carrying a ReplyKeyboardMarkup CANNOT later be edited to show an inline one.
         (Tried: send the welcome text with the panel, then edit the inline grid onto it. The edit
         is rejected and the menu ships with no buttons at all.);
      3. therefore the panel needs a message of its own. There is no arrangement in which one
         bubble holds both keyboards.

      4. BUT: `deleteMessage` does NOT take the reply keyboard down with it. The keyboard is applied
         to the chat's input area the moment the client receives the update; it is not stored on the
         message. Verified on the live bot — panel still working after its carrier was deleted.

    So: send a carrier, delete it immediately. The only cost is that the carrier is briefly visible
    while the delete round-trips. That flicker is why `_installed` exists — the panel is installed
    once per (user, locale, admin-status) per process instead of on every single `/start`, so the
    flicker is a rare one-off rather than something the user sees constantly.

    The cache is in-memory on purpose. Persisting it would mean a user who lost the panel could
    never get it back; a bot restart clearing it is the recovery path. The locale and admin flag are
    part of the key because the panel's buttons send their own localized label as plain text (the
    `MenuButton` filter matches them back), so changing either has to re-install it.

    Never "improve" this into: a carrier left in the chat (the stray `📌 Menu` bubble), a
    zero-width-blank carrier (an empty-looking bubble, and Telegram rejects truly empty text), or
    a re-send on every navigation (constant flicker).
    """
    key = (telegram_id, locale, is_admin)
    if key in _installed:
        return

    caption = t("panel.caption", locale)
    if not is_sendable_text(caption):
        logger.error("panel.caption resolved to empty/blank text (locale=%s); refusing to send", locale)
        return

    try:
        carrier = await message.answer(caption, reply_markup=main_reply_keyboard(locale, is_admin=is_admin))
    except Exception as e:
        # The panel is a nicety attached to a screen that has already rendered — a Telegram hiccup
        # here must not take the welcome message down with it. Not marking it installed means the
        # next /start retries.
        logger.error("Failed to send the reply panel: %s", e, exc_info=True)
        return

    # Installed the moment the carrier is delivered, whether or not the cleanup below succeeds.
    _installed.add(key)

    try:
        await carrier.delete()
    except Exception as e:
        # Cosmetic only: the panel is already up, the carrier just outstays its welcome this once.
        logger.warning("Could not delete the reply-panel carrier message: %s", e)
