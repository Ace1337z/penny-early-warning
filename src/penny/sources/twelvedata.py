"""TwelveData: fresh single-symbol price, volume and candles (7.x).

Finviz stays the screener and the quote feed for the whole sub-$10 universe: it
is free, fast, and returns thousands of rows per call. TwelveData is the
*freshness* source for the handful of symbols the operator actually acts on --
an alerted name, a `/check`, and the candle history behind the technicals,
Fibonacci swing and trade plan.

Two properties make that split the right one:

- The free plan allows 8 credits/minute and 800/day, so it must never be pointed
  at a 2300-symbol sweep. `RateLimiter` enforces the per-minute budget and a day
  counter enforces the daily one; both fail soft (return empty) rather than
  raising into a cycle.
- The free plan's 1-minute bars are regular-session only (`prepost=true` is
  rejected with 403 and every returned bar sits inside 09:30-16:00 ET). Finviz is
  therefore still the extended-hours source via its After-Hours Close/Change
  columns; TwelveData wins on the latest regular-session print, cumulative
  volume and candle history.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .base import BaseClient, RateLimiter, SourceError, fnum

log = logging.getLogger(__name__)

# TwelveData returns datetimes in exchange-local time. The bars carry an explicit
# America/New_York offset, which we ask for with `timezone=`, so parsing them as
# UTC would shift every intraday reading by four hours and corrupt VWAP, the
# Fibonacci swing and every rise window.
TD_ET = "America/New_York"

INTERVALS = {
    "1min": "1min", "5min": "5min", "15min": "15min", "30min": "30min",
    "1h": "1h", "1day": "1day",
}


class TwelveDataRejected(SourceError):
    """The key is invalid, exhausted or over quota."""


class TwelveDataClient(BaseClient):
    name = "twelvedata"

    def __init__(self, cfg, secrets: Optional[Iterable[str]] = None):
        super().__init__(cfg, secrets)
        self.key = cfg.raw("TWELVEDATA_KEY")
        self.base = (cfg.raw("TWELVEDATA_BASE") or "https://api.twelvedata.com").rstrip("/")
        self.max_bars = max(1, cfg.int("TWELVEDATA_MAX_BARS", 500))
        per_min = max(1, cfg.int("TWELVEDATA_MAX_PER_MIN", 8))
        self.limiter = RateLimiter(per_min, 60.0)
        self.max_per_day = max(0, cfg.int("TWELVEDATA_MAX_PER_DAY", 800))
        self._day = ""
        self._day_calls = 0

    @property
    def configured(self) -> bool:
        return bool(self.key)

    # -- quota ---------------------------------------------------------------
    def _quota_ok(self) -> bool:
        """Per-minute limiter plus a rolling daily counter. Fail soft."""
        if not self.configured:
            return False
        import time as _time
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self._day_calls = 0
        if self.max_per_day and self._day_calls >= self.max_per_day:
            log.warning("twelvedata: daily quota exhausted (%d); skipping", self._day_calls)
            return False
        try:
            self.limiter.acquire()
        except Exception as exc:  # noqa: BLE001
            log.warning("twelvedata: rate limiter failed: %s", exc)
        self._day_calls += 1
        return True

    def _get(self, path: str, **params: Any) -> Any:
        if not self._quota_ok():
            return {}
        params["apikey"] = self.key
        try:
            data = self.get_json(f"{self.base}{path}", params=params)
        except SourceError as exc:
            text = str(exc).lower()
            # `get_json` reports only "twelvedata: HTTP 429" when the credit limit
            # is hit, so the status codes are matched explicitly -- looking for the
            # word "quota" in the text misses the real exhaustion path.
            if any(c in text for c in ("401", "403", "429", "key", "quota", "credit")):
                raise TwelveDataRejected(self.error_text(exc)) from exc
            raise
        if isinstance(data, dict) and data.get("status") == "error":
            msg = str(data.get("message") or "unknown error")
            if any(k in msg.lower() for k in ("key", "quota", "credit", "plan", "permission")):
                raise TwelveDataRejected(msg)
            log.debug("twelvedata %s: %s", path, msg)
            return {}
        return data or {}

    # -- price / quote -------------------------------------------------------
    def quote(self, symbol: str) -> Optional[dict]:
        """One fresh quote in the system's quote shape.

        `previous_close` becomes `prev_close` and the daily `volume` becomes
        `cum_volume`, so a `/check` on TwelveData measures the same way one on
        Finviz does.
        """
        symbol = str(symbol or "").upper().strip()
        if not symbol:
            return None
        data = self._get("/quote", symbol=symbol)
        if not data or not data.get("close"):
            return None
        price = fnum(data.get("close"))
        if not price or price <= 0:
            return None
        prev = fnum(data.get("previous_close"))
        change_pct = fnum(data.get("percent_change"))
        if change_pct is None and prev and prev > 0:
            change_pct = (price / prev - 1.0) * 100.0
        return {
            "symbol": symbol,
            "price": price,
            "prev_close": prev,
            "cum_volume": fnum(data.get("volume")) or 0.0,
            "open_price": fnum(data.get("open")),
            "high_price": fnum(data.get("high")),
            "low_price": fnum(data.get("low")),
            "avg_volume": fnum(data.get("average_volume")),
            "market_cap": None,
            "float_shares": None,
            "bid": None,
            "ask": None,
            "change_pct": change_pct,
            "halted": False,
            "session": "regular",
            "source": self.name,
            "ts": float(data.get("timestamp") or 0) or self._now(),
        }

    def _now(self) -> float:
        import time as _time
        return _time.time()

    # -- candles -------------------------------------------------------------
    def bars(self, symbol: str, interval: str = "1min", outputsize: int = 300,
             start: Optional[datetime] = None,
             end: Optional[datetime] = None) -> list[dict]:
        """Candles in the system's `{time, open, high, low, close, volume}` shape.

        `time` is epoch seconds. TwelveData answers newest-first, so the list is
        sorted oldest-first -- callers such as `analyze()` and `anchored_vwap()`
        index `bars[-1]` as "now" and would silently read the past as the present
        if the order were left alone.
        """
        symbol = str(symbol or "").upper().strip()
        if not symbol:
            return []
        interval = INTERVALS.get(interval, interval)
        size = max(1, min(int(outputsize or 300), self.max_bars))
        params: dict[str, Any] = {"symbol": symbol, "interval": interval,
                                  "outputsize": size, "timezone": TD_ET}
        if start:
            params["start_date"] = start.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        if end:
            params["end_date"] = end.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        data = self._get("/time_series", **params)
        out: list[dict] = []
        for bar in (data.get("values") or []):
            parsed = self._bar(bar)
            if parsed:
                out.append(parsed)
        out.sort(key=lambda b: b["time"])
        return out

    def _bar(self, bar: dict) -> Optional[dict]:
        raw = str(bar.get("datetime") or "")
        if not raw:
            return None
        ts = self._parse_td_time(raw)
        if ts is None:
            return None
        close = fnum(bar.get("close"))
        if close is None:
            return None
        return {"time": ts, "open": fnum(bar.get("open")) or close,
                "high": fnum(bar.get("high")) or close,
                "low": fnum(bar.get("low")) or close,
                "close": close, "volume": fnum(bar.get("volume")) or 0.0}

    @staticmethod
    def _parse_td_time(raw: str) -> Optional[int]:
        """Exchange-local 'YYYY-MM-DD HH:MM:SS' -> epoch seconds (UTC)."""
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                naive = datetime.strptime(raw.strip(), fmt)
            except ValueError:
                continue
            try:
                from zoneinfo import ZoneInfo
                aware = naive.replace(tzinfo=ZoneInfo(TD_ET))
                return int(aware.timestamp())
            except Exception:  # noqa: BLE001 - no tzdata: fall back to the exchange offset
                return int(naive.replace(tzinfo=timezone(timedelta(hours=-4))).timestamp())
        log.debug("twelvedata: unparsed bar time %r", raw)
        return None

    # -- health --------------------------------------------------------------
    def health(self) -> tuple[bool, str]:
        if not self.configured:
            return False, "not configured (optional)"
        try:
            data = self._get("/quote", symbol="AAPL")
            if data.get("close"):
                return True, f"ok (AAPL {data['close']})"
            return False, "no quote returned"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
