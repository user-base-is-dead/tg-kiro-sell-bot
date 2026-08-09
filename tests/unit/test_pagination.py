from __future__ import annotations

from app.utils.pagination import Page


def test_total_pages_and_offset():
    p = Page(page=1, page_size=8, total_items=20)
    assert p.total_pages == 3
    assert p.offset == 0
    assert p.has_next is True
    assert p.has_prev is False


def test_middle_page():
    p = Page(page=2, page_size=8, total_items=20)
    assert p.offset == 8
    assert p.has_prev is True
    assert p.has_next is True


def test_clamps_out_of_range_page():
    p = Page(page=99, page_size=8, total_items=20)
    assert p.clamped_page == 3
    assert p.has_next is False

    p2 = Page(page=0, page_size=8, total_items=20)
    assert p2.clamped_page == 1


def test_empty_collection_has_one_page():
    p = Page(page=1, page_size=8, total_items=0)
    assert p.total_pages == 1
    assert p.has_next is False
    assert p.has_prev is False
