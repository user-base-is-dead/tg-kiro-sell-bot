from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    page: int
    page_size: int
    total_items: int

    @property
    def total_pages(self) -> int:
        return max(1, (self.total_items + self.page_size - 1) // self.page_size)

    @property
    def offset(self) -> int:
        return (self.clamped_page - 1) * self.page_size

    @property
    def clamped_page(self) -> int:
        return min(max(1, self.page), self.total_pages)

    @property
    def has_prev(self) -> bool:
        return self.clamped_page > 1

    @property
    def has_next(self) -> bool:
        return self.clamped_page < self.total_pages
