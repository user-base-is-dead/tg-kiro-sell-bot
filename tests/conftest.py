from __future__ import annotations

import pytest
from aiogram import Dispatcher

from app.main import _include_routers


@pytest.fixture(scope="session")
def router_order() -> tuple[str, ...]:
    """Router names in the order main.py registers them.

    Session-scoped because it has to be: the routers are module-level singletons and aiogram
    raises "Router is already attached" on the second Dispatcher, so the real wiring can only be
    built once per process. Every test that asserts on registration order shares this one build.
    """
    dp = Dispatcher()
    _include_routers(dp)
    return tuple(r.name for r in dp.sub_routers)
