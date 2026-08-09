from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCALES_DIR = Path(__file__).parent
_SUPPORTED = ("en",)
_FALLBACK = "en"


@lru_cache
def _load(locale: str) -> dict[str, Any]:
    path = _LOCALES_DIR / f"{locale}.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _get(d: dict[str, Any], dotted_key: str) -> str | None:
    node: Any = d
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def t(key: str, locale: str = _FALLBACK, **vars: Any) -> str:
    """t('menu.products', locale='hi', name='Alex') — dotted-key lookup with {var} substitution.
    Falls back to English, then to the raw key (logged) so a missing translation never crashes a screen."""
    locale = locale if locale in _SUPPORTED else _FALLBACK

    value = _get(_load(locale), key)
    if value is None and locale != _FALLBACK:
        value = _get(_load(_FALLBACK), key)

    if value is None:
        logger.warning("i18n: missing key %r (locale=%s)", key, locale)
        return key

    try:
        return value.format(**vars)
    except (KeyError, IndexError):
        logger.warning("i18n: bad vars for key %r: %s", key, vars)
        return value


def supported_locales() -> tuple[str, ...]:
    return _SUPPORTED
