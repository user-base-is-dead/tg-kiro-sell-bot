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
