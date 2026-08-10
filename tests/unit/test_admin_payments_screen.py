from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

import app.bot.handlers.admin.payments as payments


class _Session:
    """Only _describe touches the session, and only to look up a wallet."""

    def __init__(self, wallet=None) -> None:
        self._wallet = wallet

    async def get(self, _model, _pk):
        return self._wallet


def _patch_pending(monkeypatch, txns: list) -> None:
    class _Repo:
        def __init__(self, _session) -> None:
            pass

        async def list_pending_topups(self):
            return txns

    monkeypatch.setattr(payments, "WalletRepo", _Repo)


def _patch_users(monkeypatch, user=None) -> None:
    class _Repo:
        def __init__(self, _session) -> None:
            pass

        async def get_by_id(self, _uid):
            return user

    monkeypatch.setattr(payments, "UserRepo", _Repo)


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_screen_always_offers_a_way_back(monkeypatch) -> None:
    """This screen was a dead end — the only control was an inert "— none —" row, so the admin
    had to retype /start to leave it."""
    _patch_pending(monkeypatch, [])

    _, markup = await payments._render(_Session())

    targets = [data for _, data in _buttons(markup)]
    assert "nav:admin_panel" in targets, "Back must return to the admin panel"


@pytest.mark.asyncio
async def test_empty_screen_explains_why_it_is_empty(monkeypatch) -> None:
    """Zero pending requests is the healthy state now that crypto auto-credits, so the screen has
    to say so rather than look broken."""
    _patch_pending(monkeypatch, [])

    text, _ = await payments._render(_Session())

    assert "Nothing to review" in text
    assert "crypto" in text.lower()
    assert "How this works" in text, "the screen must explain what Approve/Reject do"


@pytest.mark.asyncio
async def test_pending_request_is_listed_with_approve_and_reject(monkeypatch) -> None:
    txn = SimpleNamespace(
        id=7,
        amount_minor=1500,
        wallet_id=1,
        proof="paid via UPI",
        created_at=datetime.datetime(2026, 8, 10, 14, 30),
    )
    wallet = SimpleNamespace(user_id=3, currency="USD")
    _patch_pending(monkeypatch, [txn])
    _patch_users(monkeypatch, SimpleNamespace(username="buyer", telegram_id=999))

    text, markup = await payments._render(_Session(wallet))

    assert "@buyer" in text
    assert "paid via UPI" in text
    labels = [label for label, _ in _buttons(markup)]
    assert any("#7" in label for label in labels)
    assert any("Reject" in label for label in labels)


@pytest.mark.asyncio
async def test_approve_is_green_and_reject_is_red(monkeypatch) -> None:
    """Colour is the thing standing between a tap and crediting the wrong request."""
    txn = SimpleNamespace(id=7, amount_minor=1500, wallet_id=1, proof=None, created_at=None)
    _patch_pending(monkeypatch, [txn])
    _patch_users(monkeypatch, None)

    _, markup = await payments._render(_Session(SimpleNamespace(user_id=3, currency="USD")))

    styles = {b.text[:1]: b.style for row in markup.inline_keyboard for b in row}
    assert styles["✅"] == "success"
    assert styles["❌"] == "danger"


@pytest.mark.asyncio
async def test_missing_user_does_not_break_the_screen(monkeypatch) -> None:
    """A deleted user must not take down the whole review queue."""
    txn = SimpleNamespace(id=8, amount_minor=100, wallet_id=1, proof=None, created_at=None)
    _patch_pending(monkeypatch, [txn])
    _patch_users(monkeypatch, None)

    text, _ = await payments._render(_Session(None))

    assert "unknown user" in text
