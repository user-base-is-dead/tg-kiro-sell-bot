from __future__ import annotations

# U+2800 BRAILLE PATTERN BLANK, ×30.
#
# A message bubble and its inline keyboard share one width, and Telegram takes the wider of the two.
# A screen whose longest text line is short therefore squeezes its own buttons into a narrow strip
# with the labels floating in empty space. Appending this as the last line sets a floor on the
# bubble width; screens already wider than it are unaffected.
#
# Ordinary spaces are trimmed from message text and would widen nothing. U+2800 is printable,
# survives the trim, and renders as nothing — the same character and the same reason as BLANK in
# app/bot/panel.py, which uses a single one to carry the reply keyboard on a blank-looking message.
#
# 30 clears the longest real line on the screens this is used on (23 chars, "Category:
# Uncategorized") while staying under the ~35 where the pad itself wraps on a narrow phone and
# shows up as an unexplained blank gap under the copy.
PAD = "⠀" * 30


def as_admin_wrote_it(message) -> str:
    """The admin's message, formatting and all, ready to be re-sent as HTML.

    Whatever they typed is what the buyer gets: a monospace block stays a monospace block (tappable
    to copy in every Telegram client), bold stays bold, plain text stays plain. The bot used to wrap
    every delivery in <code> on the buyer's behalf, which forced the copy-box onto content that was
    never meant to be one and quietly stripped any formatting the admin had applied.

    `html_text` also escapes the literal <, > and & that a password is entitled to contain — the
    reason this cannot simply be `message.text`.
    """
    return (message.html_text or "").strip()
