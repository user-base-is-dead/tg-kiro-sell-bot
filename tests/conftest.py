from __future__ import annotations

import pytest
from aiogram import Dispatcher

from app.main import _include_routers


@pytest.fixture(scope="session")
def dispatcher() -> Dispatcher:
    """The one and only Dispatcher for the whole test session.

    It has to be shared: the routers are module-level singletons and aiogram raises "Router is
    already attached" the second time one is included. Any test that needs real routing — filter
    chains, handler resolution, registration order — feeds updates into this instance.

    Middlewares are deliberately not registered, so tests inject `session`/`user` as kwargs
    instead of standing up a database.
    """
    dp = Dispatcher()
    _include_routers(dp)
    return dp


@pytest.fixture(scope="session")
def router_order(dispatcher: Dispatcher) -> tuple[str, ...]:
    """Router names in the order main.py registers them."""
    return tuple(r.name for r in dispatcher.sub_routers)
