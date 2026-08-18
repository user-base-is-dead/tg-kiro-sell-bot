"""A broadcast could not point at anything.

Announcing a product meant writing "open /products to grab it" and asking the reader to go and find
it again — the walk where interest is lost. The product announcements created from the product
screen already carried a Buy Now button; a hand-composed broadcast had no way to attach one.

A not-yet-released product is the one an announcement is *most* likely to be about, so it can be
attached too — but it gets a View button, because a Buy Now that answers "unknown action" is worse
than no button at all.
"""

from __future__ import annotations

import json

from app.bot.handlers.admin.broadcast import (
    _attached_label,
    _button_label,
    _buttons_json,
    _control_text,
)

_ON_SALE = {"id": 7, "name": "Netflix 1 Month", "coming_soon": False}
_COMING_SOON = {"id": 9, "name": "Spotify Family", "coming_soon": True}


def test_no_product_means_no_buttons() -> None:
    """Plenty of broadcasts are not about a product. Attaching one is optional."""
    assert _buttons_json(None) is None


def test_an_on_sale_product_gets_a_buy_now_button() -> None:
    rows = json.loads(_buttons_json(_ON_SALE))
    assert rows == [[{"text": "🛒 Buy Now", "callback_data": "prod:buy:7::1"}]]


def test_an_unreleased_product_gets_a_view_button_instead() -> None:
    rows = json.loads(_buttons_json(_COMING_SOON))
    assert rows[0][0]["text"] == "👀 View product"
    assert rows[0][0]["callback_data"] == "prod:view:9::1"
    assert "buy" not in rows[0][0]["callback_data"]


def test_the_labels_match_what_the_confirm_screen_promises() -> None:
    """The admin is told which button the post will carry before sending; if the two drifted apart
    the preview would be a lie."""
    for product in (_ON_SALE, _COMING_SOON):
        assert _button_label(product) in _control_text(2, product)
        assert product["name"] in _control_text(2, product)


def test_the_confirm_screen_says_nothing_about_products_when_none_is_attached() -> None:
    assert "Attached" not in _control_text(2, None)


# ---- The attached-product button names the product in full ----


def test_the_attached_button_shows_the_whole_name() -> None:
    """It was cut at 20 characters — inside the part that tells two products apart. "Kiro Pro Max"
    and "Kiro Pro Plus" both came out as "Kiro Pro"-something, on the one screen whose job is
    confirming which product is about to go out to every user."""
    long_name = {"id": 3, "name": "Chatgpt plus 1months (apple pay)", "coming_soon": False}

    assert _attached_label(long_name) == "🛒 Product: Chatgpt plus 1months (apple pay)"


def test_a_name_past_telegram_s_button_limit_is_trimmed_not_lost() -> None:
    """64 characters is Telegram's own ceiling on button text. Past it the send fails and the admin
    gets no screen at all, which is worse than an ellipsis."""
    label = _attached_label({"id": 4, "name": "N" * 200, "coming_soon": False})

    assert len(label) == 64
    assert label.endswith("…")
