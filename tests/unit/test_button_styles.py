from __future__ import annotations

import pytest

from app.bot.keyboards.common import confirm_row, nav_row
from app.bot.keyboards.main_menu import (
    language_inline_keyboard,
    main_inline_keyboard,
    main_reply_keyboard,
)
from app.bot.keyboards.styles import DANGER, PRIMARY, SUCCESS, btn
from app.locales.i18n import supported_locales

VALID = {PRIMARY, SUCCESS, DANGER, None}


def _styles(markup) -> list[str | None]:
    return [b.style for row in markup.inline_keyboard for b in row]


def _reply_styles(markup) -> list[str | None]:
    return [b.style for row in markup.keyboard for b in row]


def test_btn_rejects_unknown_style() -> None:
    # Telegram rejects the whole sendMessage call on a bad style, so this has to fail at build time
    # rather than at the API boundary.
    with pytest.raises(ValueError, match="unsupported button style"):
        btn("x", "cb", "blue")


@pytest.mark.parametrize("is_admin", [False, True])
def test_main_menu_styles_are_valid(is_admin: bool) -> None:
    assert set(_styles(main_inline_keyboard("en", is_admin=is_admin))) <= VALID


def test_language_keyboard_styles_are_valid() -> None:
    assert set(_styles(language_inline_keyboard())) <= VALID


def test_admin_row_is_only_present_for_admins() -> None:
    admin_labels = [b.text for row in main_inline_keyboard("en", is_admin=True).inline_keyboard for b in row]
    user_labels = [b.text for row in main_inline_keyboard("en", is_admin=False).inline_keyboard for b in row]
    assert any("Admin" in label for label in admin_labels)
    assert not any("Admin" in label for label in user_labels)


@pytest.mark.parametrize("is_admin", [False, True])
def test_reply_keyboard_styles_are_valid(is_admin: bool) -> None:
    assert set(_reply_styles(main_reply_keyboard("en", is_admin=is_admin))) <= VALID


@pytest.mark.parametrize("locale", supported_locales())
def test_reply_and_inline_menus_show_the_same_entries(locale: str) -> None:
    """The bottom panel and the in-message menu are two renderings of one menu, so their labels must
    stay identical — MenuButton matches a reply press by its label text.

    The panel's leading Start row is excluded: it navigates to the inline menu, which cannot carry a
    link to itself. The community row is excluded for the mirror-image reason: it is a url button,
    and a reply button can only send its label as text — there is nothing for it to open."""
    from app.locales.i18n import t

    inline = main_inline_keyboard(locale, is_admin=True)
    reply = main_reply_keyboard(locale, is_admin=True)
    reply_labels = [b.text for row in reply.keyboard for b in row]
    inline_labels = [
        b.text for row in inline.inline_keyboard for b in row if b.text != t("menu.community", locale)
    ]
    assert reply_labels[0] == t("menu.start", locale)
    assert reply_labels[1:] == inline_labels


@pytest.mark.parametrize("locale", supported_locales())
def test_start_is_the_first_row_of_the_panel_only(locale: str) -> None:
    from app.locales.i18n import t

    reply = main_reply_keyboard(locale, is_admin=False)
    assert [b.text for b in reply.keyboard[0]] == [t("menu.start", locale)]
    inline_labels = [b.text for row in main_inline_keyboard(locale, is_admin=False).inline_keyboard for b in row]
    assert t("menu.start", locale) not in inline_labels


def test_main_menu_is_one_flat_color() -> None:
    # Inline is uniformly blue; the panel is uniformly unstyled. Start is included in the second
    # assertion on purpose — as the lone styled row it was the unreadable case that started this.
    assert set(_styles(main_inline_keyboard("en", is_admin=True))) == {PRIMARY}
    assert set(_reply_styles(main_reply_keyboard("en", is_admin=True))) == {None}


@pytest.mark.parametrize("locale", supported_locales())
def test_reply_keyboard_labels_match_the_locale(locale: str) -> None:
    # Each button sends its own label as plain text, so the labels must be the ones MenuButton
    # resolves for that same locale.
    from app.locales.i18n import t

    labels = {b.text for row in main_reply_keyboard(locale, is_admin=False).keyboard for b in row}
    assert t("menu.products", locale) in labels
    assert t("menu.topup", locale) in labels


def test_reply_keyboard_is_persistent_and_resized() -> None:
    kb = main_reply_keyboard("en", is_admin=False)
    assert kb.is_persistent is True
    assert kb.resize_keyboard is True


def test_top_level_screens_offer_exactly_one_way_back() -> None:
    """Every destination the main menu can reach has to be leaveable from the screen itself. On a
    top-level screen Back and Home are the same jump, so only Back is drawn."""
    from app.bot.keyboards.common import back_keyboard
    from app.bot.keyboards.orders import order_history_list
    from app.bot.keyboards.products import category_grid
    from app.locales.i18n import t
    from app.utils.pagination import Page

    screens = [
        category_grid([], "en"),
        order_history_list([], Page(page=1, page_size=6, total_items=0), "en"),
        language_inline_keyboard("en"),
        back_keyboard("en"),
    ]
    for markup in screens:
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert labels.count(t("menu.back", "en")) == 1
        assert t("menu.home", "en") not in labels


def test_nav_row_back_is_danger_and_home_is_primary() -> None:
    back, home = nav_row("en", back_target="categories", home=True)
    assert (back.style, home.style) == (DANGER, PRIMARY)


def test_confirm_row_is_green_then_red() -> None:
    confirm, cancel = confirm_row("en", "ok", "no")
    assert (confirm.style, cancel.style) == (SUCCESS, DANGER)
