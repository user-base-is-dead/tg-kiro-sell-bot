from __future__ import annotations

from app.bot.keyboards.products import category_back_target


def test_a_categorised_product_goes_back_to_its_folder() -> None:
    assert category_back_target(7) == "cat-7"


def test_a_loose_product_goes_back_to_the_store_root() -> None:
    """nav.py resolves this token with int(target.removeprefix("cat-")). "cat-None" raised
    ValueError there, so buying a product filed outside every category and pressing Back showed
    the generic "something went wrong" alert instead of a screen."""
    assert category_back_target(None) == "categories"


def test_every_target_this_builds_survives_navs_parser() -> None:
    """The bug was not a wrong string, it was a string nav.py could not parse. Assert the contract
    directly rather than the spelling."""
    for category_id in (None, 1, 9_999_999_999):
        target = category_back_target(category_id)
        if target.startswith("cat-"):
            int(target.removeprefix("cat-"))  # must not raise
        else:
            assert target == "categories"
