from __future__ import annotations

from app.bot.handlers.admin.panel import _PANEL_TEXT, _panel_keyboard


def test_panel_has_no_payments_button() -> None:
    """Payments opened the *manual* top-up queue. Top-ups are automatic crypto now, and the screen
    itself concedes an empty list is the healthy state."""
    targets = [b.callback_data for row in _panel_keyboard("en").inline_keyboard for b in row]

    assert not any(t.startswith("apay") for t in targets), "the dead Payments button is still here"


def test_panel_text_no_longer_advertises_payments() -> None:
    assert "Payments" not in _PANEL_TEXT


def test_panel_text_exists_exactly_once_in_source() -> None:
    """The body was pasted verbatim into both the command handler and the nav handler. Two copies
    drift; this keeps there being one."""
    import pathlib

    source = pathlib.Path(_panel_keyboard.__globals__["__file__"]).read_text(encoding="utf-8")

    assert source.count("Quick Commands") == 1, "the panel body must live in one constant"


def test_every_panel_button_still_has_a_home() -> None:
    """Removing a button must not orphan the rest — every remaining destination is still reachable."""
    targets = [b.callback_data for row in _panel_keyboard("en").inline_keyboard for b in row]

    assert len(targets) == len(set(targets)), "a duplicate destination means a copy-paste slip"
    assert "nav:home" in targets
