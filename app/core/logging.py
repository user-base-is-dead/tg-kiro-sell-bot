from __future__ import annotations

import logging
import re

_TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")
_REDACTED = "***REDACTED***"


class SecretRedactionFilter(logging.Filter):
    """Strips bot tokens / DB URLs with credentials out of log lines before they hit any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if _TOKEN_RE.search(msg) or "://" in msg and "@" in msg:
            record.msg = _TOKEN_RE.sub(_REDACTED, str(record.msg))
            record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(SecretRedactionFilter())

    root.handlers.clear()
    root.addHandler(handler)

    # aiogram / httpx are noisy at INFO with full update payloads
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
