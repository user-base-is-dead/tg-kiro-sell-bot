from __future__ import annotations

import logging

from aiogram.types import ReplyKeyboardMarkup

from app.bot.keyboards.main_menu import main_reply_keyboard

logger = logging.getLogger(__name__)

# Telegram identities (telegram_id, locale, is_admin) whose panel this process has already
# handed out. Deliberately in-memory and deliberately NOT persisted — see `panel_markup`.
_installed: set[tuple[int, str, bool]] = set()


def reset_installed_panels() -> None:
    """Test hook. Production never needs this: a bot restart clears the set by itself."""
    _installed.clear()


def panel_markup(
    telegram_id: int, locale: str, *, is_admin: bool, force: bool = False
) -> ReplyKeyboardMarkup | None:
    """The bottom panel, to be attached to a message the caller is sending anyway. `None` means the
    user already has it and the caller should send their message with its normal markup.

    READ THIS BEFORE CHANGING ANYTHING HERE. Every "obvious" alternative has been tried on the real
    bot and failed. The Bot API constraints:

      1. one `sendMessage` cannot carry an inline keyboard and a reply keyboard together;
      2. `editMessageText`/`editMessageReplyMarkup` accept only an *inline* keyboard — a message
         that was sent carrying a ReplyKeyboardMarkup CANNOT later be edited to show an inline one;
      3. therefore the panel needs a message of its own. There is no arrangement in which one
         bubble holds both keyboards.
      4. That message must STAY IN THE CHAT. This is the one that cost the most time: the old
         implementation sent a throwaway carrier and deleted it immediately, on the belief that
         `deleteMessage` leaves the keyboard up. That is only true on Telegram Desktop, which
         applies the keyboard to the input area on receipt and keeps it there. On the mobile
         clients the keyboard is bound to its message, so deleting the carrier takes the panel
         down with it — the reported symptom was the panel flashing up for about a second and then
         vanishing. There is no delay or ordering that fixes this; the carrier simply cannot be
         deleted.

    So the panel rides on real content instead: `/start`'s welcome text carries it, and the inline
    main menu moves to a second message (it has to be the inline one that lives separately, because
    nav edits it — see constraint 2). No throwaway bubble, nothing to clean up, nothing to flicker.

    The cache is in-memory on purpose. Persisting it would mean a user who lost the panel could
    never get it back. The locale and admin flag are part of the key because the panel's buttons
    send their own localized label as plain text (the `MenuButton` filter matches them back), so
    changing either has to re-issue it.

    `force=True` skips the cache: the `/start` command uses it so a user whose client dropped the
    keyboard has a way to ask for it back. The panel's own Start *button* must NOT force — whoever
    pressed it plainly still has the panel.
    """
    key = (telegram_id, locale, is_admin)
    if key in _installed and not force:
        return None

    _installed.add(key)
    return main_reply_keyboard(locale, is_admin=is_admin)
