"""Shared plumbing for data sources: rate limiting, caching, CSV parsing, normalization."""

from __future__ import annotations

import csv
import io
import logging
import threading
import time
from typing import Any, Callable, Iterable, Optional

from ..util import ET, http_request, now_et, safe_div, scrub

log = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """A recoverable source failure; callers fail soft."""


class RateLimiter:
    """Simple thread-safe limiter: at most `rate` calls per `per_seconds`."""

    def __init__(self, rate: int, per_seconds: float = 60.0):
        self.rate = max(1, int(rate))
        self.per = float(per_seconds)
        self._times: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < self.per]
                if len(self._times) < self.rate:
                    self._times.append(now)
                    return
                wait = self.per - (now - self._times[0]) + 0.01
            time.sleep(max(0.01, wait))


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float) -> Optional[Any]:
        with self._lock:
            item = self._data.get(key)
        if item is None:
            return None
        ts, val = item
        if time.monotonic() - ts > ttl:
            return None
        return val

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def parse_csv(text: str) -> list[dict[str, str]]:
    """Parse CSV text into dicts, tolerating a BOM and blank lines."""
    if text is None:
        return []
    s = text.lstrip("\ufeff")
    if not s.strip():
        return []
    reader = csv.DictReader(io.StringIO(s))
    rows = []
    for row in reader:
        if row is None:
            continue
        clean = {(k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
                 for k, v in row.items()}
        if any(v for v in clean.values()):
            rows.append(clean)
    return rows


def looks_like_html(text: str) -> bool:
    head = (text or "").lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or "<html" in head


def fnum(value: Any) -> Optional[float]:
    """Tolerant number parsing: strips %, $, commas, multipliers like K/M/B/T."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("%", "").replace("$", "")
    if not s or s in {"-", "n/a", "N/A", "NA", "--", "None", "nan"}:
        return None
    mult = 1.0
    if s and s[-1].upper() in {"K", "M", "B", "T"}:
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[s[-1].upper()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def pct_to_fraction(value: Any) -> Optional[float]:
    """Moomoo returns ratios as percentages (1.23 = 1.23%)."""
    v = fnum(value)
    return None if v is None else v / 100.0


class BaseClient:
    """Common HTTP behaviour: timeouts, retries, secret scrubbing, optional limiter."""

    name = "base"

    def __init__(self, cfg, secrets: Optional[Iterable[str]] = None):
        self.cfg = cfg
        self._secrets = list(secrets or [])
        self._session = None
        self._cache = TTLCache()
        self._api_calls = 0
        self._lock = threading.Lock()

    @property
    def secrets(self) -> list[str]:
        return [s for s in self._secrets if s]

    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": "penny-early-warning/1.0"})
        return self._session

    def count_call(self, n: int = 1) -> None:
        with self._lock:
            self._api_calls += n

    @property
    def api_calls(self) -> int:
        with self._lock:
            return self._api_calls

    def reset_calls(self) -> None:
        with self._lock:
            self._api_calls = 0

    def request(self, method: str, url: str, **kwargs):
        kwargs.setdefault("timeout", 20.0)
        self.count_call()
        resp = http_request(method, url, session=self.session(), secrets=self.secrets, **kwargs)
        return resp

    def get_text(self, url: str, **kwargs) -> str:
        resp = self.request("GET", url, **kwargs)
        if resp.status_code in (401, 403):
            raise SourceError(f"{self.name}: rejected (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise SourceError(f"{self.name}: HTTP {resp.status_code}")
        return resp.text

    def get_json(self, url: str, **kwargs) -> Any:
        resp = self.request("GET", url, **kwargs)
        if resp.status_code in (401, 403):
            raise SourceError(f"{self.name}: rejected (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise SourceError(f"{self.name}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"{self.name}: invalid JSON") from exc

    def post_json(self, url: str, body: Any, **kwargs) -> Any:
        resp = self.request("POST", url, json_body=body, **kwargs)
        if resp.status_code in (401, 403):
            raise SourceError(f"{self.name}: rejected (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise SourceError(f"{self.name}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"{self.name}: invalid JSON") from exc

    def health(self) -> tuple[bool, str]:
        return True, "ok"

    def error_text(self, exc: Exception) -> str:
        return scrub(str(exc), self.secrets)
