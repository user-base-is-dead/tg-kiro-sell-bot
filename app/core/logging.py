from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = Path("bot.log")
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3

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

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redaction = SecretRedactionFilter()

    root.handlers.clear()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(redaction)
    root.addHandler(stream)

    # A crash surfaces to the user as a one-line "something went wrong" alert; the traceback that
    # explains it only ever existed in the console, which is gone the moment the window is closed
    # or scrolled. Rotating file so it cannot grow unbounded on a long-running bot.
    try:
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
        )
    except OSError:
        # A read-only or otherwise unwritable working directory must not stop the bot booting.
        root.warning("Could not open %s for logging; console only", LOG_FILE)
    else:
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction)
        root.addHandler(file_handler)

    # aiogram / httpx are noisy at INFO with full update payloads
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
