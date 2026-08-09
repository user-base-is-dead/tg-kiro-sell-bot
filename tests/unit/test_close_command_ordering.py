from __future__ import annotations

from app.bot.handlers.admin.support import close_from_topic
from app.bot.handlers.admin.support import router as admin_support_router
from app.bot.handlers.support.relay import group_relay
from app.bot.handlers.support.relay import router as relay_router


def test_close_is_claimed_before_the_relay_catch_all(router_order: tuple[str, ...]) -> None:
    """`/close` must be handled by the admin router, not fall through to group_relay — aiogram
    stops at the first match, and group_relay would mirror the literal text "/close" into the
    user's DM as if support had said it. The guarantee is purely the include order, so it breaks
    silently the moment someone reorders _include_routers."""
    assert router_order.index(admin_support_router.name) < router_order.index(relay_router.name)


def test_relay_stays_last(router_order: tuple[str, ...]) -> None:
    assert router_order[-1] == relay_router.name


def test_close_lives_on_the_admin_router_not_the_relay() -> None:
    admin_callbacks = {h.callback for h in admin_support_router.message.handlers}
    assert close_from_topic in admin_callbacks
    assert group_relay not in admin_callbacks
