from __future__ import annotations

import datetime

from aiogram.types import Chat, Document, Message, PhotoSize, Voice
from aiogram.types import User as TgUser

from app.bot.handlers.admin.broadcast import (
    MAX_PARTS,
    TITLE_MAX_CHARS,
    _derive_title,
    _part_label,
    _writing_keyboard,
)


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _msg(**kwargs) -> Message:
    base = dict(
        message_id=1,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=1, type="private"),
        from_user=TgUser(id=1, is_bot=False, first_name="A"),
    )
    return Message(**{**base, **kwargs})


# -- part labels: how each content type is described in the running counter --


def test_text_part_is_labelled_with_its_text() -> None:
    assert _part_label(_msg(text="Big sale today")) == "💬 Big sale today"


def test_long_text_label_is_truncated() -> None:
    label = _part_label(_msg(text="x" * 100))
    assert label.endswith("…")
    assert len(label) < 100


def test_newlines_are_flattened_in_the_label() -> None:
    """The label goes into a numbered list, so an embedded newline would break the layout."""
    assert _part_label(_msg(text="line one\nline two")) == "💬 line one line two"


def test_photo_is_recognised_as_media_not_empty_text() -> None:
    """The bug this replaces: a photo has no .text, so it used to be rejected as an empty
    message and silently dropped."""
    photo = [PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]
    assert _part_label(_msg(photo=photo)) == "🖼️ Photo"


def test_document_label_uses_the_file_name() -> None:
    doc = Document(file_id="f", file_unique_id="u", file_name="prices.pdf")
    assert _part_label(_msg(document=doc)) == "📎 prices.pdf"


def test_voice_note_is_recognised() -> None:
    voice = Voice(file_id="f", file_unique_id="u", duration=3)
    assert _part_label(_msg(voice=voice)) == "🎤 Voice"


# -- title derivation --


def test_title_prefers_the_first_text_part() -> None:
    """A broadcast that opens with a photo still deserves a readable title, so the first *text*
    part wins over the media part that precedes it."""
    assert _derive_title(["🖼️ Photo", "💬 Big sale today"]) == "Big sale today"


def test_title_falls_back_to_the_first_part_when_there_is_no_text() -> None:
    assert _derive_title(["🖼️ Photo", "🎬 Video"]) == "🖼️ Photo"


def test_long_title_is_truncated_with_ellipsis() -> None:
    title = _derive_title(["💬 " + "x" * 200])
    assert len(title) == TITLE_MAX_CHARS
    assert title.endswith("…")


def test_empty_parts_still_yield_a_title() -> None:
    """`title` is not nullable — an empty string would fail the insert at send time."""
    assert _derive_title([]) == "Broadcast"
    assert _derive_title(["💬 "]) == "Broadcast"


# -- keyboard --


def test_done_sits_beside_abort_from_the_first_screen() -> None:
    """The instructions say "tap Done when you've finished", so Done has to be there to tap —
    including on the empty first screen."""
    assert _labels(_writing_keyboard()) == ["✅ Done", "❌ Abort"]


def test_part_cap_is_sane() -> None:
    assert 1 < MAX_PARTS <= 20
