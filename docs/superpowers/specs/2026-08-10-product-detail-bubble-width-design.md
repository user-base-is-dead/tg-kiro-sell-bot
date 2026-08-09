# Product Detail Bubble Width — Design

**Date:** 2026-08-10
**Status:** Approved, pending implementation plan

## Problem

On the admin product detail screen the message bubble renders much narrower than the column of
buttons under it, and the buttons are squeezed to match. A bubble and its attached inline keyboard
share one width, and Telegram sets that width from whichever side is wider. Here the text is the
narrower side, so it decides.

The longest real line in `_render_detail` (`app/bot/handlers/admin/products.py:205-214`) is
`Category: Uncategorized` — 23 characters. Every other line is shorter: `Stock: 0`,
`Warranty: 1 days`, `Fulfillment: AUTO`. So a 23-character line is what pins `📦 Add Stock`,
`🗑️ Delete` and the rest into a narrow strip, with the label floating in a wide empty field.

Nothing is broken — the screen is legible and every button works. This is purely how it looks.

## Decision

Append one padding line of invisible characters to the detail text, sized past the longest real
line. The bubble stretches to the pad, the keyboard follows the bubble, and the copy an admin reads
is untouched.

Rejected alternatives:

| Option | Why not |
|---|---|
| Visible divider rule under the product name | Widens the bubble and adds real structure, but it is permanent visible chrome on a screen meant for a quick glance. |
| Longer self-documenting field lines | Matches the pattern the product *list* screen already uses for its `Buttons:` legend, but adds reading weight to a screen whose job is a fast scan of seven facts. |

Padding wins because the complaint is about width, and width is the only thing it changes.

## Scope

`_render_detail` in `app/bot/handlers/admin/products.py` only.

The user detail screen (`app/bot/handlers/admin/users.py:163`) builds a similarly short field list
and has the same latent narrowness. It is deliberately left alone — it was not the screen
complained about, and a `PAD` constant is cheap to copy if it ever is. The other five admin render
functions (dashboard, payments, settings, gifts, categories) already carry wide explanatory copy
and need no padding.

## Implementation

A module-level constant in `products.py`:

```python
PAD = "⠀" * 30  # U+2800 ×30 — forces the bubble wider than its longest real line
```

Appended as the final line of the `text` f-string in `_render_detail`, after `Delivery info:`. Last
position keeps the field list in its existing reading order, and puts the pad where a trailing
blank line would sit naturally anyway.

### Character choice

U+2800 BRAILLE PATTERN BLANK. Telegram trims ordinary spaces from message text, so a run of spaces
would collapse and widen nothing. U+2800 is a printable character that survives the trim and renders
as nothing.

This is the same character and the same reason as `BLANK` in `app/bot/panel.py:14`, which already
documents the trimming behaviour. `products.py` gets its own `PAD` constant rather than importing
`BLANK`: `BLANK` is a single character with one specific job — the text of the reply-panel carrier
message — and a separate name at the use site says what this one is for without the two drifting
into each other.

### Width

30 characters, against a longest real line of 23.

The lower bound is 24: anything at or below 23 changes nothing. The upper bound is the point where
the pad itself wraps — if the pad exceeds the width Telegram will render, it breaks into a second
invisible line and shows up as an unexplained blank gap under the fields. On a narrow phone bubble
that limit is around 35 characters. 30 buys a clear widening with room on both sides of it.

## Testing

A new test in `tests/integration/test_product_edit.py`, alongside the existing detail-screen
coverage, calling `_render_detail` against a product created through `create_product` with the
`sqlite_sessionmaker` fixture. The product is given a deliberately short name, so that the title
line cannot be what supplies the width and the assertions stay about the pad.

Two measurement details the test has to respect:

- **Measure the pad line, not every line.** The title line carries `<b>` tags and a status emoji,
  and a long product name would make it the longest line on its own. Assertions 1 and 2 target the
  pad line and the seven field lines specifically.
- **Markup is not width.** `<b>Kiro Pro</b>` is 15 raw characters but renders as 8, so raw string
  length overstates the title line. This is another reason the title line is excluded rather than
  measured.

The assertions:

1. **The bubble is wide.** The text contains a line of at least 24 U+2800 characters — strictly
   wider than the longest real field line, which is what the fix is for.
2. **The pad is invisible and does not disturb the copy.** Removing the pad line leaves the seven
   field lines byte-identical to an unpadded render, and none of them exceeds 23 characters —
   proving the width comes from padding and not from the copy having been rewritten.
3. **The pad does not wrap.** The pad line is at most 35 characters.

Asserting a range rather than `== 30` keeps the test from breaking if the pad is later nudged, while
still failing if it is removed, or grown past the wrap point.

## Non-goals

- The user detail screen, or any other admin screen.
- A shared `pad_to_width()` helper. One call site does not need one; the second one can extract it.
- Any change to the fields shown, their order, their wording, or the buttons.
