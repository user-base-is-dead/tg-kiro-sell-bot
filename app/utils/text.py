from __future__ import annotations

import re

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


def escape_html(text: object) -> str:
    """Make arbitrary text safe to drop into an HTML message, without mangling punctuation.

    `html.escape` defaults to `quote=True`, which also rewrites ' and " as numeric entities. That
    matters here because the text going through this is prose somebody typed — a decline reason, a
    payout note — and "the customer's card" came out as "the customer&#x27;s card". Quote escaping
    exists for values placed inside HTML *attributes*; nothing here ever is, so it is switched off.

    Only <, > and & are rewritten, which is exactly what Telegram requires of text that is not part
    of a tag.
    """
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if text is not None
        else ""
    )


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


# Telegram refuses a message whose text runs over 4096 UTF-16 units once the HTML is parsed — and
# refuses it whole, so nothing at all arrives.
MESSAGE_LIMIT = 4000


def message_size(text: str) -> int:
    """How much of the limit `text` uses, never under what Telegram will count.

    UTF-16 units because that is Telegram's unit: an emoji is two of them, which `len` counts as one.
    Measured on the HTML as written, so tags and entities count too — strictly more than the parsed
    text Telegram measures, which is the safe direction to be wrong in.
    """
    return len(text.encode("utf-16-le")) // 2


_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")
_VOID_TAGS = {"br", "hr"}


def _hard_cut(text: str, room: int) -> int:
    """The longest prefix of `text` that fits in `room`, backed off so it never ends inside a tag
    or an HTML entity — half of `<code>` or of `&amp;` would make the whole message unparseable."""
    used = cut = 0
    for i, ch in enumerate(text):
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > room:
            break
        cut = i + 1
    head = text[:cut]
    if head.rfind("<") > head.rfind(">"):
        cut = head.rfind("<")
    amp = head.rfind("&")
    if amp > head.rfind(";") and cut - amp <= 10:
        cut = min(cut, amp)
    return max(cut, 1)


def _cut(text: str, room: int) -> list[str]:
    """Cut `text` into pieces of at most `room`: on line breaks where possible, anywhere if a
    single line is longer than that."""
    pieces: list[str] = []
    current: str | None = None
    for line in text.split("\n"):
        candidate = line if current is None else f"{current}\n{line}"
        if message_size(candidate) <= room:
            current = candidate
            continue
        if current is not None:
            pieces.append(current)
        while message_size(line) > room:
            cut = _hard_cut(line, room)
            pieces.append(line[:cut])
            line = line[cut:]
        current = line
    if current:
        pieces.append(current)
    return pieces


def _balance(pieces: list[str]) -> list[str]:
    """Close every tag a piece leaves open, and reopen it at the start of the next one, so each
    piece is valid HTML on its own — a `<pre>` cut in two becomes two copy-boxes, not a parse error."""
    balanced: list[str] = []
    carried: list[tuple[str, str]] = []
    for piece in pieces:
        text = "".join(opener for _, opener in carried) + piece
        stack: list[tuple[str, str]] = []
        for match in _TAG.finditer(text):
            name = match.group(2).lower()
            if name in _VOID_TAGS:
                continue
            if not match.group(1):
                stack.append((name, match.group(0)))
                continue
            for depth in range(len(stack) - 1, -1, -1):
                if stack[depth][0] == name:
                    del stack[depth]
                    break
        balanced.append(text + "".join(f"</{name}>" for name, _ in reversed(stack)))
        carried = stack
    return balanced


def _fit(text: str, budget: int) -> list[str]:
    """`text` as pieces that each fit `budget` as sent — the closing and reopening tags included."""
    if message_size(text) <= budget:
        return [text]
    room = budget
    while True:
        pieces = _balance(_cut(text, room))
        if room <= 64 or all(message_size(piece) <= budget for piece in pieces):
            return pieces
        room = room * 3 // 4


def pack_messages(head: str, items: list[str], tail: str = "", *, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Lay `head`, the `items` (one per line) and `tail` out over as few messages as fit.

    No message ever goes over `limit`. An item is kept whole whenever it fits in a message of its
    own — a credential split across two messages is two useless fragments — and is only cut, on a
    line break where it can be, when it is bigger than a whole message. The head opens the first
    message and the tail closes the last.
    """
    pieces = [piece for item in items for piece in _fit(item, limit)]
    messages: list[str] = []
    prefix, body = head, []
    for piece in pieces:
        if body and message_size(prefix + "\n".join([*body, piece])) > limit:
            messages.append(prefix + "\n".join(body))
            prefix, body = "", []
        elif not body and prefix and message_size(prefix + piece) > limit:
            messages.extend(_fit(prefix.rstrip("\n"), limit))
            prefix = ""
        body.append(piece)
    last = prefix + "\n".join(body)
    if tail and message_size(last + tail) > limit:
        if last.strip():
            messages.extend(_fit(last.rstrip("\n"), limit))
        messages.extend(_fit(tail.strip("\n"), limit))
    else:
        messages.extend(_fit(last + tail, limit))
    return [message for message in messages if message.strip()]
