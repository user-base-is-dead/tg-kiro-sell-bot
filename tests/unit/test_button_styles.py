from __future__ import annotations

import pytest

from app.bot.keyboards.common import confirm_row, nav_row
from app.bot.keyboards.main_menu import language_inline_keyboard, main_inline_keyboard
from app.bot.keyboards.styles import DANGER, PRIMARY, SUCCESS, btn

VALID = {PRIMARY, SUCCESS, DANGER, None}


def _styles(markup) -> list[str | None]:
    return [b.style for row in markup.inline_keyboard for b in row]


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


def test_every_main_menu_button_is_styled() -> None:
    # A lone unstyled button among styled ones was the unreadable case that started this. The menu
    # is no longer one flat color, but nothing in it may fall back to the transparent default —
    # url button included.
    assert None not in _styles(main_inline_keyboard("en", is_admin=True))


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
