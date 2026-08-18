from __future__ import annotations



def _targets(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_product_list_offers_a_way_back() -> None:
    """These four screens were dead ends: the only way out was retyping /admin."""
    from app.bot.handlers.admin.products import _tools_rows

    # The tools row is shared by the list, the search results and every category folder, so this
    # one assertion covers the way out of all three.
    targets = [b.callback_data for row in _tools_rows() for b in row if b.callback_data]

    assert "nav:admin_panel" in targets


def test_category_list_offers_a_way_back() -> None:
    from app.bot.handlers.admin.categories import _list_keyboard

    assert "nav:admin_panel" in _targets(_list_keyboard([]))


def test_order_list_offers_a_way_back() -> None:
    from app.bot.handlers.admin.orders import _list_keyboard

    assert "nav:admin_panel" in _targets(_list_keyboard([]))


def test_user_detail_offers_exactly_one_way_back() -> None:
    """It used to carry `🔙 Back to list` *and* a plain `🔙 Back` to the admin panel — two red
    buttons reading almost the same, where the second skipped past the list being navigated. The
    list is one tap from the panel, so the panel shortcut cost more than it saved."""
    from types import SimpleNamespace

    from app.bot.handlers.admin.users import _detail_keyboard
    from app.database.models.user import UserStatus

    target = SimpleNamespace(id=1, status=UserStatus.ACTIVE)
    markup = _detail_keyboard(target, page=3)
    targets = _targets(markup)

    assert "auser:list::3" in targets, "Back must return to the page the profile was opened from"
    assert "nav:admin_panel" not in targets, "the duplicate Back is gone"
    backs = [b.text for row in markup.inline_keyboard for b in row if "Back" in b.text]
    assert len(backs) == 1, f"one Back button, got {backs}"


async def test_gift_list_offers_a_way_back(monkeypatch) -> None:
    """Found while fixing the other four: the Gift Codes list had no escape either."""
    import app.bot.handlers.admin.gifts as gifts

    class _EmptyRepo:
        def __init__(self, _session):
            pass

        async def list_all(self):
            return []

    monkeypatch.setattr(gifts, "GiftRepo", _EmptyRepo)

    _text, markup = await gifts._render_list(None)

    assert "nav:admin_panel" in _targets(markup)


def test_back_button_is_red() -> None:
    """nav_row's convention: leaving a screen is always red, on every screen in the bot."""
    from app.bot.handlers.admin.categories import _list_keyboard

    back = [
        b
        for row in _list_keyboard([]).inline_keyboard
        for b in row
        if b.callback_data == "nav:admin_panel"
    ]
    assert back and back[0].style == "danger"
