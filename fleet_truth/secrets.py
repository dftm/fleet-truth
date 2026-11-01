"""Environment-variable secrets. Values are read lazily and are never printed or logged.

Config files hold only the NAME of an env var. `get(name)` returns the value or None.
`scrub(text)` replaces any known secret value found in text with `***`; every string that can
reach a log line, the DB, the page or an alert goes through it.
"""
from __future__ import annotations

import logging
import os
import re
import threading

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_MIN_SCRUB_LEN = 4  # shorter values would mangle ordinary text

_lock = threading.Lock()
_names: set[str] = set()


def register(*names: str) -> None:
    """Remember env var names whose values must be scrubbed from output."""
    with _lock:
        _names.update(n for n in names if n and ENV_NAME.match(n))


def get(name: str) -> str | None:
    """Value of the env var, or None when unset or empty."""
    if not ENV_NAME.match(name or ""):
        return None
    v = os.environ.get(name)
    return v if v else None


def scrub(text: object) -> str:
    s = text if isinstance(text, str) else str(text)
    with _lock:
        names = sorted(_names)
    for n in names:
        v = os.environ.get(n)
        if v and len(v) >= _MIN_SCRUB_LEN and v in s:
            s = s.replace(v, "***")
    return s


class ScrubFilter(logging.Filter):
    """Logging filter that scrubs the formatted message of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string must not break logging
            msg = str(record.msg)
        record.msg = scrub(msg)
        record.args = None
        return True


def install_log_scrubber(logger: logging.Logger | None = None) -> None:
    lg = logger or logging.getLogger()
    if not any(isinstance(f, ScrubFilter) for f in lg.filters):
        lg.addFilter(ScrubFilter())
    for h in lg.handlers:
        if not any(isinstance(f, ScrubFilter) for f in h.filters):
            h.addFilter(ScrubFilter())
