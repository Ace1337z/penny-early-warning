"""Logging setup: rotating file log plus console, with secret scrubbing."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path
from typing import Iterable, Optional

from .util import scrub


class ScrubFilter(logging.Filter):
    """Removes tokens/keys from every record before it is emitted."""

    def __init__(self, secrets: Optional[Iterable[str]] = None):
        super().__init__()
        self._secrets: list[str] = []
        self.set_secrets(secrets or [])

    def set_secrets(self, secrets: Iterable[str]) -> None:
        self._secrets = [s for s in secrets if s and len(str(s)) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        cleaned = scrub(msg, self._secrets)
        if cleaned != msg:
            record.msg = cleaned
            record.args = ()
        if record.exc_info:
            pass
        return True


_scrub_filter: Optional[ScrubFilter] = None


def setup_logging(home: Path, level: str = "INFO", secrets: Optional[Iterable[str]] = None,
                  console: bool = True) -> ScrubFilter:
    global _scrub_filter
    home = Path(home)
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    _scrub_filter = ScrubFilter(secrets)

    fh = logging.handlers.RotatingFileHandler(
        log_dir / "penny.log", maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(_scrub_filter)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.addFilter(_scrub_filter)
        root.addHandler(ch)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    return _scrub_filter


def update_secrets(secrets: Iterable[str]) -> None:
    if _scrub_filter is not None:
        _scrub_filter.set_secrets(secrets)


def redact_paths(text: str) -> str:
    """Extra safety net used before writing error text to the database."""
    return scrub(text)
