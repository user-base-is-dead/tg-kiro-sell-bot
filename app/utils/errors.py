from __future__ import annotations

from typing import Any


class UserError(Exception):
    """Expected, user-facing failure (out of stock, invalid gift code, insufficient balance, ...).
    Caught by ErrorMiddleware and rendered via i18n — never a raw traceback to the user."""

    def __init__(self, i18n_key: str, **vars: Any) -> None:
        self.i18n_key = i18n_key
        self.vars = vars
        super().__init__(i18n_key)


class SystemError_(Exception):
    """Unexpected failure. ErrorMiddleware shows a generic message and logs the full trace."""
