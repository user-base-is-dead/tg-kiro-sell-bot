from __future__ import annotations

import pathlib
import re

APP = pathlib.Path(__file__).resolve().parents[2] / "app"

_BUILT = re.compile(r'NavCB\(target=["\']([\w\-]+)["\']')
_FILTERED = re.compile(r'F\.target\s*==\s*["\']([\w\-]+)["\']')
_NAV_EQ = re.compile(r'target\s*==\s*["\']([\w\-]+)["\']')
_NAV_PREFIX = re.compile(r'target\.startswith\(["\']([\w\-]+)["\']')


def _scan() -> tuple[set[str], set[str], tuple[str, ...]]:
    built: set[str] = set()
    handled: set[str] = set()
    prefixes: set[str] = set()

    for path in APP.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        built |= set(_BUILT.findall(source))
        # A target is handled either by a router filtering on it, or by nav.py's dispatch chain.
        handled |= set(_FILTERED.findall(source))
        if path.name == "nav.py":
            handled |= set(_NAV_EQ.findall(source))
            prefixes |= set(_NAV_PREFIX.findall(source))

    return built, handled, tuple(prefixes)


def test_every_nav_target_has_a_handler() -> None:
    """A NavCB target nobody handles falls through to "This action is no longer available" —
    a dead button that looks fine in review and only fails in the user's hands. This caught a real
    one: the gift screens shipped `target="main"`, which no router has ever resolved (it is
    "home"). Static, but the failure it prevents is silent and user-facing.
    """
    built, handled, prefixes = _scan()
    unhandled = {t for t in built if t not in handled and not t.startswith(prefixes)}
    assert not unhandled, (
        f"NavCB target(s) with no handler: {sorted(unhandled)}. "
        f"Add a router filtering on it, or a branch in nav.py's on_nav."
    )


def test_scan_actually_finds_targets() -> None:
    """Guards the test above from silently passing if the regexes ever stop matching — an empty
    scan would make the real assertion vacuously true."""
    built, handled, _ = _scan()
    assert "home" in built
    assert {"gift", "topup", "support"} <= handled
