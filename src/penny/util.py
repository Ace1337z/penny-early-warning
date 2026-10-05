"""Shared helpers: time zones, sessions, HTTP with retry, scrubbing, formatting."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
import time
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


class _USEastern(tzinfo):
    """Fallback US/Eastern when the IANA database is unavailable (e.g. no tzdata)."""

    def __init__(self) -> None:
        super().__init__()

    @staticmethod
    def _dst_bounds(year: int) -> tuple[datetime, datetime]:
        def nth_sunday(month: int, n: int) -> date:
            d = date(year, month, 1)
            first = d + timedelta(days=(6 - d.weekday()) % 7)
            return first + timedelta(days=7 * (n - 1))
        start = datetime.combine(nth_sunday(3, 2), dtime(2, 0))
        end = datetime.combine(nth_sunday(11, 1), dtime(2, 0))
        return start, end

    def utcoffset(self, dt):  # noqa: D102
        if dt is None:
            return timedelta(hours=-5)
        naive = dt.replace(tzinfo=None)
        start, end = self._dst_bounds(naive.year)
        return timedelta(hours=-4) if start <= naive < end else timedelta(hours=-5)

    def dst(self, dt):  # noqa: D102
        if dt is None:
            return timedelta(0)
        return self.utcoffset(dt) - timedelta(hours=-5)

    def tzname(self, dt):  # noqa: D102
        return "EDT" if self.dst(dt) else "EST"


def _load_et():
    try:
        return ZoneInfo("America/New_York")
    except Exception as exc:  # noqa: BLE001
        log.warning("zoneinfo database unavailable (%s); using the built-in US/Eastern rules. "
                    "Install the 'tzdata' package for full accuracy.", exc)
        return _USEastern()


ET = _load_et()
UTC = timezone.utc

# US market sessions in US/Eastern.
PRE_OPEN = dtime(4, 0)
REGULAR_OPEN = dtime(9, 30)
REGULAR_CLOSE = dtime(16, 0)
POST_CLOSE = dtime(20, 0)

SESSION_PRE = "pre"
SESSION_REGULAR = "regular"
SESSION_POST = "post"
SESSION_CLOSED = "closed"


def now_et() -> datetime:
    return datetime.now(tz=ET)


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def to_et(dt: Optional[datetime] = None) -> datetime:
    if dt is None:
        return now_et()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET)


def epoch_ms(dt: Optional[datetime] = None) -> int:
    dt = dt or now_utc()
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def session_for(dt: Optional[datetime] = None) -> str:
    """Return the trading session name for a US/Eastern datetime."""
    dt = to_et(dt or now_et())
    t = dt.time()
    if PRE_OPEN <= t < REGULAR_OPEN:
        return SESSION_PRE
    if REGULAR_OPEN <= t < REGULAR_CLOSE:
        return SESSION_REGULAR
    if REGULAR_CLOSE <= t < POST_CLOSE:
        return SESSION_POST
    return SESSION_CLOSED


def session_end(dt: Optional[datetime] = None) -> datetime:
    """End-of-session datetime in ET for the session that contains `dt`."""
    dt = to_et(dt or now_et())
    s = session_for(dt)
    d = dt.date()
    if s == SESSION_PRE:
        return datetime.combine(d, REGULAR_OPEN, tzinfo=ET)
    if s == SESSION_REGULAR:
        return datetime.combine(d, REGULAR_CLOSE, tzinfo=ET)
    if s == SESSION_POST:
        return datetime.combine(d, POST_CLOSE, tzinfo=ET)
    # Closed: next session end is the upcoming regular close (or today's if before 4am).
    if dt.time() < PRE_OPEN:
        return datetime.combine(d, REGULAR_CLOSE, tzinfo=ET)
    return datetime.combine(d + timedelta(days=1), REGULAR_CLOSE, tzinfo=ET)


def is_trading_day(d: Optional[date] = None) -> bool:
    d = d or now_et().date()
    return d.weekday() < 5 and d not in us_market_holidays(d.year)


# --- US market holidays (C10: built-in list for the current year) -----------

_FIXED_HOLIDAYS = [(1, 1), (7, 4), (12, 25)]


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """nth (1-based) weekday of a month. weekday: Monday=0."""
    d = date(year, month, 1)
    shift = (weekday - d.weekday()) % 7
    d = d + timedelta(days=shift + 7 * (n - 1))
    return d


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    shift = (d.weekday() - weekday) % 7
    return d - timedelta(days=shift)


def _observed(d: date) -> date:
    if d.weekday() == 5:  # Saturday -> Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday -> Monday
        return d + timedelta(days=1)
    return d


_HOLIDAY_CACHE: dict[int, set[date]] = {}


def us_market_holidays(year: int) -> set[date]:
    if year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[year]
    days = {_observed(date(year, m, d)) for m, d in _FIXED_HOLIDAYS}
    days.add(_nth_weekday(year, 1, 0, 3))   # MLK Day
    days.add(_nth_weekday(year, 2, 0, 3))   # Presidents' Day
    days.add(_last_weekday(year, 5, 0))     # Memorial Day
    days.add(_observed(date(year, 6, 19)))  # Juneteenth
    days.add(_nth_weekday(year, 9, 0, 1))   # Labor Day
    days.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    days.add(_observed(date(year, 12, 25)))
    _HOLIDAY_CACHE[year] = days
    return days


# --- formatting -------------------------------------------------------------

def money(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    if abs(x) >= 1000:
        return f"${x:,.0f}"
    if abs(x) >= 1:
        return f"${x:,.2f}"
    return f"${x:.4f}".rstrip("0").rstrip(".")


def compact(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    a = abs(x)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{x / div:.1f}{suf}".replace(".0" + suf, suf)
    return f"{x:.0f}"


def pct(x: Optional[float], digits: int = 1) -> str:
    if x is None:
        return "n/a"
    return f"{x:+.{digits}f}%"


def num(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return "n/a"
    return f"{x:.{digits}f}"


def age_str(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    if seconds < 48 * 3600:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    try:
        if b == 0:
            return default
        return a / b
    except (TypeError, ZeroDivisionError):
        return default


def percentile(values: Iterable[float], p: float) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return vals[int(k)]
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def median(values: Iterable[float]) -> Optional[float]:
    return percentile(values, 0.5)


# --- secret scrubbing -------------------------------------------------------

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(auth=)[^&\s\"']+", re.I),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I),
    re.compile(r"([?&](?:token|api_key|apikey|key|access_token)=)[^&\s\"']+", re.I),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),  # Telegram bot tokens
]


def scrub(text: Any, extra: Optional[Iterable[str]] = None) -> str:
    """Remove tokens/keys from any string before logging or messaging."""
    s = "" if text is None else str(text)
    for pat in _SECRET_PATTERNS:
        s = pat.sub(lambda m: (m.group(1) if m.groups() else "***") + "***", s)
    if extra:
        for secret in extra:
            if secret and len(str(secret)) >= 6:
                s = s.replace(str(secret), "***")
    return s


def scrub_deep(obj: Any, extra: Optional[Iterable[str]] = None) -> Any:
    if isinstance(obj, dict):
        return {k: scrub_deep(v, extra) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_deep(v, extra) for v in obj]
    if isinstance(obj, str):
        return scrub(obj, extra)
    return obj


def mask(value: Optional[str]) -> str:
    """Show the first 4 characters of a secret, never the rest."""
    if not value:
        return "(unset)"
    return value[:4] + "*" * 6


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- HTTP with retry and back-off ------------------------------------------

class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = "", retry_after: Optional[float] = None):
        self.status = status
        self.url = url
        self.body = body
        self.retry_after = retry_after
        super().__init__(f"HTTP {status} for {scrub(url)}: {scrub(body)[:200]}")


def http_request(
    method: str,
    url: str,
    *,
    session=None,
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    data: Any = None,
    json_body: Any = None,
    timeout: float = 20.0,
    retries: int = 2,
    backoff: float = 1.0,
    secrets: Optional[Iterable[str]] = None,
    sleep: Callable[[float], None] = time.sleep,
):
    """HTTP call with 2 retries, exponential back-off and token scrubbing.

    Returns the `requests.Response`. Raises HttpError on the final failure.
    """
    import requests  # local import keeps import-time light

    sess = session or requests
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = sess.request(
                method,
                url,
                headers=headers,
                params=params,
                data=data,
                json=json_body,
                timeout=timeout,
            )
        except Exception as exc:  # network error
            last_exc = exc
            if attempt >= retries:
                raise HttpError(0, url, f"network error: {exc}") from exc
            sleep(backoff * (2 ** attempt) + random.random() * 0.2)
            continue

        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            wait = float(ra) if ra and ra.isdigit() else backoff * (2 ** attempt)
            if attempt >= retries:
                raise HttpError(429, url, resp.text, wait)
            sleep(wait)
            continue
        if resp.status_code >= 500:
            last_exc = HttpError(resp.status_code, url, resp.text)
            if attempt >= retries:
                raise last_exc
            sleep(backoff * (2 ** attempt))
            continue
        return resp
    raise HttpError(0, url, str(last_exc))


def parse_json_lenient(text: str) -> Any:
    """Parse JSON, tolerating code fences and surrounding prose."""
    if text is None:
        raise ValueError("empty response")
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(s[start:end + 1])
    raise ValueError("no JSON object found")
