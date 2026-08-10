from __future__ import annotations

import pytest

from app.bot.panel import is_sendable_text, reset_installed_panels, send_reply_panel


@pytest.fixture(autouse=True)
def _clean_install_cache():
    reset_installed_panels()
    yield
    reset_installed_panels()


class _Sent:
    def __init__(self, text: str, reply_markup, *, fail_delete: bool = False) -> None:
        self.text = text
        self.reply_markup = reply_markup
        self.deleted = False
        self._fail_delete = fail_delete

    async def delete(self) -> None:
        if self._fail_delete:
            raise RuntimeError("message can't be deleted")
        self.deleted = True


class _Message:
    """Stand-in for aiogram's Message that records what the panel helper sent."""

    def __init__(self, *, fail_send: bool = False, fail_delete: bool = False) -> None:
        self.sent: list[_Sent] = []
        self._fail_send = fail_send
        self._fail_delete = fail_delete

    async def answer(self, text: str, reply_markup=None):  # noqa: ANN001 - test double
        if self._fail_send:
            raise RuntimeError("telegram is down")
        message = _Sent(text, reply_markup, fail_delete=self._fail_delete)
        self.sent.append(message)
        return message


@pytest.mark.asyncio
async def test_panel_is_sent_with_a_reply_keyboard() -> None:
    message = _Message()

    await send_reply_panel(message, "en", is_admin=False, telegram_id=1)

    assert len(message.sent) == 1
    assert message.sent[0].reply_markup is not None
    assert message.sent[0].reply_markup.keyboard, "the panel must carry keyboard rows"


@pytest.mark.asyncio
async def test_the_carrier_never_stays_in_the_chat() -> None:
    """The carrier exists only to deliver the keyboard to the chat's input area; deleting it does
    not take the keyboard down. Leaving it is what produced the stray `📌 Menu` bubble under the
    main menu, so its removal is pinned here rather than left to a comment."""
    message = _Message()

    await send_reply_panel(message, "en", is_admin=False, telegram_id=1)

    assert message.sent[0].deleted, "the panel carrier must never be left in the chat"


@pytest.mark.asyncio
async def test_panel_is_installed_once_per_user() -> None:
    """The carrier is briefly visible while the delete round-trips, so it must not be re-sent on
    every `/start`. Repeat calls for the same user are a no-op — no send, no flicker."""
    message = _Message()

    for _ in range(5):
        await send_reply_panel(message, "en", is_admin=False, telegram_id=1)

    assert len(message.sent) == 1


@pytest.mark.asyncio
async def test_a_locale_change_reinstalls_the_panel() -> None:
    """The buttons send their own localized label as plain text, so stale labels stop matching the
    `MenuButton` filter. The locale is part of the install key precisely to force a re-send."""
    message = _Message()

    await send_reply_panel(message, "en", is_admin=False, telegram_id=1)
    await send_reply_panel(message, "hi", is_admin=False, telegram_id=1)

    assert len(message.sent) == 2


@pytest.mark.asyncio
async def test_gaining_admin_reinstalls_the_panel() -> None:
    message = _Message()

    await send_reply_panel(message, "en", is_admin=False, telegram_id=1)
    await send_reply_panel(message, "en", is_admin=True, telegram_id=1)

    assert len(message.sent) == 2
    assert len(message.sent[1].reply_markup.keyboard) > len(message.sent[0].reply_markup.keyboard)


@pytest.mark.asyncio
async def test_each_user_gets_their_own_install() -> None:
    message = _Message()

    await send_reply_panel(message, "en", is_admin=False, telegram_id=1)
    await send_reply_panel(message, "en", is_admin=False, telegram_id=2)

    assert len(message.sent) == 2


@pytest.mark.asyncio
async def test_a_failed_send_never_breaks_the_caller_and_is_retried() -> None:
    """The panel is a nicety attached to a screen that has already rendered — a Telegram hiccup here
    must not take down the welcome message. A failed send must also not count as installed."""
    failing = _Message(fail_send=True)

    await send_reply_panel(failing, "en", is_admin=False, telegram_id=1)  # must not raise
    assert failing.sent == []

    working = _Message()
    await send_reply_panel(working, "en", is_admin=False, telegram_id=1)
    assert len(working.sent) == 1, "a failed install must be retried, not cached"


@pytest.mark.asyncio
async def test_a_failed_delete_still_counts_as_installed() -> None:
    """The keyboard is live the moment the carrier is delivered. If the cleanup fails the bubble
    lingers once, but re-sending would only add another one."""
    message = _Message(fail_delete=True)

    await send_reply_panel(message, "en", is_admin=False, telegram_id=1)  # must not raise
    await send_reply_panel(message, "en", is_admin=False, telegram_id=1)

    assert len(message.sent) == 1


def test_is_sendable_text_rejects_empty_whitespace_and_zero_width() -> None:
    assert not is_sendable_text("")
    assert not is_sendable_text("   ")
    assert not is_sendable_text("\n\t ")
    assert not is_sendable_text("⁠")
    assert not is_sendable_text("​‌  ⁠")
    assert not is_sendable_text(None)


def test_is_sendable_text_accepts_real_content() -> None:
    assert is_sendable_text("📌 Menu")
    assert is_sendable_text("  hello  ")
