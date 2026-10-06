"""Finviz Elite client.

The links never change; only the token at the end (`auth=`) is replaceable, at any time,
without code changes or a restart. The token is read from configuration on every request
and is never cached in code, logged or placed in messages (6.5/5.2).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Optional
from urllib.parse import urlencode

from .base import BaseClient, RateLimiter, SourceError, fnum, looks_like_html, parse_csv
from ..util import session_for

log = logging.getLogger(__name__)

# Finviz screener "signals" (the `s=` query parameter). Names are user friendly so
# the operator can reorder or trim them with one configuration key.
SIGNALS: dict[str, str] = {
    "top_gainers": "ta_topgainers",
    "top_losers": "ta_toplosers",
    "new_high": "ta_newhigh",
    "new_low": "ta_newlow",
    "most_active": "ta_mostactive",
    "most_volatile": "ta_mostvolatile",
    "unusual_volume": "ta_unusualvolume",
    "overbought": "ta_overbought",
    "oversold": "ta_oversold",
    "insider_buying": "it_latestbuys",
    "insider_selling": "it_latestsales",
    "upgrades": "n_upgrades",
    "downgrades": "n_downgrades",
    # Intraday top gainers/losers (Elite). These catch a stock that is being
    # bought *right now*, before it reaches the daily top-gainers screen.
    "top_gainers_1m": "ta_topgainers_1m",
    "top_gainers_5m": "ta_topgainers_5m",
    "top_gainers_15m": "ta_topgainers_15m",
    "top_gainers_30m": "ta_topgainers_30m",
    "top_gainers_1h": "ta_topgainers_1h",
    "top_losers_1m": "ta_toplosers_1m",
    "top_losers_5m": "ta_toplosers_5m",
    "top_losers_15m": "ta_toplosers_15m",
    "top_losers_30m": "ta_toplosers_30m",
    "top_losers_1h": "ta_toplosers_1h",
}
DEFAULT_SIGNALS = ("top_gainers", "new_high", "most_active", "unusual_volume",
                   "overbought", "oversold", "insider_buying")

# Only the top gainers/losers bases honour an intraday timeframe suffix on the
# Elite export endpoint. Every other base silently ignores the suffix and
# returns the whole universe instead (thousands of rows), so those are refused.
INTRADAY_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h")
INTRADAY_BASES = ("ta_topgainers", "ta_toplosers")

# Views that render a fixed column set and ignore `c=` (Overview 111, Valuation
# 121, Ownership 131, Performance 141, Financial 161, Technical 171). Requesting
# custom columns against one of these silently drops them, which once zeroed
# Volume and stopped every tier and build from firing, so a custom column request
# always falls back to the custom view (152).
FIXED_VIEWS = {"111", "121", "131", "141", "161", "171"}
DEFAULT_VIEW = "152"

# Columns the quote shape reads. A long-lived config.env (the updater keeps the
# configuration) can still hold the old column set that stopped at 66 (Change)
# and omitted Volume, which silently zeroes Volx and the after-hours price. Any
# custom column request is merged with these so the feed always carries them.
REQUIRED_COLUMNS: tuple[int, ...] = (
    67,                    # Volume
    71, 72,                # After-Hours Close / Change
    81,                    # Prev Close
    86, 87, 88,            # Open / High / Low
    90, 91, 92, 93, 94,    # Performance 5m..2h
    95, 96, 97, 98, 99,    # Performance 4H..1Y
)


def merge_columns(columns: str) -> str:
    """Return `columns` plus every column the quote shape needs, in order."""
    seen: list[int] = []
    for part in str(columns or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            cid = int(part)
        except ValueError:
            continue
        if cid not in seen:
            seen.append(cid)
    for cid in REQUIRED_COLUMNS:
        if cid not in seen:
            seen.append(cid)
    return ",".join(str(c) for c in seen)

# Finviz intraday performance columns, keyed by the scorer's window minutes.
PERF_COLUMNS: tuple[tuple[int, str], ...] = (
    (5, "Performance (5 Minutes)"),
    (15, "Performance (15 Minutes)"),
    (60, "Performance (1 Hour)"),
    (240, "Performance (4 Hours)"),
)


def intraday_signal_ok(code: str) -> bool:
    """True when `code` honours an intraday timeframe (or has none)."""
    for tf in INTRADAY_TIMEFRAMES:
        suffix = "_" + tf
        if code.endswith(suffix):
            return code[: -len(suffix)] in INTRADAY_BASES
    return True


def avg_volume_shares(row: dict[str, str]) -> Optional[float]:
    """Average daily volume in raw shares.

    Finviz's `Average Volume` column is in thousands (583.28 == 583.28K) while
    the test doubles and other feeds provide `Avg Volume` in raw shares.
    """
    thousands = row_num(row, "Average Volume")
    if thousands is not None:
        return thousands * 1000.0
    return row_num(row, "Avg Volume")

# Fixed link table (5.2). Adding a link needs no new authentication code.
FINVIZ_LINKS: dict[str, str] = {
    "screener": "/export/screener",
    "portfolio": "/export/portfolio",
    "stock": "/export/stock",
    "groups": "/export/groups",
    "options": "/export/options",
    "latest-filings": "/export/latest-filings",
    "news": "/export/news",
    "insiders": "/export/insiders",
    "managers": "/export/managers",
    "funds": "/export/funds",
    "calendar/economic": "/export/calendar/economic",
}


class FinvizTokenRejected(SourceError):
    """The token is invalid or expired; Finviz steps pause until it changes."""


class FinvizClient(BaseClient):
    name = "finviz"

    def __init__(self, cfg, secrets: Optional[Iterable[str]] = None):
        super().__init__(cfg, secrets)
        self.base = str(cfg.raw("FINVIZ_BASE") or "https://elite.finviz.com").rstrip("/")
        self.limiter = RateLimiter(rate=max(1, cfg.int("FINVIZ_MAX_PER_MIN", 30)), per_seconds=60.0)
        self._token_rejected = False
        self._rejected_token = ""

    # -- token --------------------------------------------------------------
    def _token(self) -> str:
        """Always read the token from the live configuration."""
        token = str(self.cfg.raw("FINVIZ_TOKEN") or "").strip()
        if not token:
            raise FinvizTokenRejected("finviz: FINVIZ_TOKEN is not set")
        # A changed token clears the rejection state without a restart.
        if self._token_rejected and token != self._rejected_token:
            log.info("finviz: token changed; resuming Finviz steps")
            self._token_rejected = False
            self._rejected_token = ""
        return token

    @property
    def token_rejected(self) -> bool:
        return self._token_rejected

    def _reject(self) -> None:
        self._token_rejected = True
        try:
            self._rejected_token = str(self.cfg.raw("FINVIZ_TOKEN") or "")
        except Exception:  # noqa: BLE001
            self._rejected_token = ""
        raise FinvizTokenRejected("finviz: token rejected")

    # -- one function for every link ---------------------------------------
    def fetch(self, link: str, *, ttl: float = 0.0, **params: Any) -> str:
        """Fetch a Finviz link and return CSV text.

        `link` is a key of FINVIZ_LINKS. `params` become query parameters; the token is
        appended last as `auth`. Repeated identical requests are cached for `ttl` seconds.
        """
        if link not in FINVIZ_LINKS:
            raise SourceError(f"finviz: unknown link '{link}'")
        if self._token_rejected:
            raise FinvizTokenRejected("finviz: steps paused after token rejection")

        token = self._token()
        path = FINVIZ_LINKS[link]
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        cache_key = f"{link}?{urlencode(sorted(clean.items()))}"
        if ttl > 0:
            cached = self._cache.get(cache_key, ttl)
            if cached is not None:
                return cached

        self.limiter.acquire()
        url = f"{self.base}{path}"
        query = dict(clean)
        query["auth"] = token
        try:
            resp = self.request("GET", url, params=query, timeout=25.0, retries=2)
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"finviz: {self.error_text(exc)}") from exc

        if resp.status_code in (401, 403):
            self._reject()
        if resp.status_code >= 400:
            raise SourceError(f"finviz: HTTP {resp.status_code}")

        text = resp.text
        if looks_like_html(text):
            # HTML instead of CSV means the token is invalid or expired.
            self._reject()

        if ttl > 0:
            self._cache.set(cache_key, text)
        return text

    def fetch_csv(self, link: str, *, ttl: float = 0.0, **params: Any) -> list[dict[str, str]]:
        rows = parse_csv(self.fetch(link, ttl=ttl, **params))
        if not rows:
            raise SourceError(f"finviz: '{link}' returned no columns")
        return rows

    # -- use cases ----------------------------------------------------------
    def screener(self, *, tickers: str = "", filters: Optional[str] = None,
                 view: str = "",
                 columns: str = "", signal: str = "", order: str = "",
                 ttl: float = 0.0) -> list[dict[str, str]]:
        # `filters=None` means "use the configured sub-price screen"; an explicit
        # "" means unfiltered (used for single-symbol lookups such as verify).
        view = view or self.cfg.raw("FINVIZ_VIEW") or DEFAULT_VIEW
        if columns and view in FIXED_VIEWS:
            # That view ignores c=; without this the requested columns (including
            # Volume) would be dropped and no tier or build could ever fire.
            log.warning("finviz: view %s ignores custom columns; using %s", view, DEFAULT_VIEW)
            view = DEFAULT_VIEW
        params = {
            "v": view,
            "f": self.cfg.raw("FINVIZ_FILTERS") if filters is None else filters,
        }
        if tickers:
            params["t"] = tickers
        if columns:
            # Always carry the quote columns, even if an old config dropped them.
            params["c"] = merge_columns(columns)
        if signal:
            params["s"] = signal
        if order:
            params["o"] = order
        return self.fetch_csv("screener", ttl=ttl, **params)

    def universe(self, ttl: float = 300.0) -> list[dict[str, str]]:
        """Universe rows for the sub-price screen (6.2). Doubles as the quote feed."""
        return self.screener(filters=None,
                             columns=self.cfg.raw("FINVIZ_COLUMNS"),
                             ttl=ttl)

    # -- market feed (Finviz is the only market data source) ----------------
    def movers(self, session: Optional[str] = None, top_n: int = 200,
               signals: Optional[Iterable[str]] = None) -> list[dict[str, str]]:
        """Rows for every configured market-mover screen: top gainers, new highs,
        unusual volume, overbought, oversold, most active, recent insider buying,
        plus the intraday (1m/5m/15m) top-gainers screens that catch a stock
        being bought right now.

        An intraday timeframe is only honoured by the top-gainers/losers bases;
        anything else silently returns the whole universe, so those are skipped.

        `top_n` bounds each individual screen, not the merged result: a single
        list-wide cap used to cut off the intraday top-gainers, which run last, so
        the momentum-build signal never fired.
        """
        session = session or session_for()
        if session == "closed":
            return []
        if signals is not None:
            names = list(signals)
        else:
            names = self.cfg.list("FINVIZ_SIGNALS") + self.cfg.list("FINVIZ_INTRADAY_SIGNALS")
        if not names:
            names = list(DEFAULT_SIGNALS)
        ttl = max(0.0, self.cfg.float("FINVIZ_MOVERS_TTL", 15.0))
        filters = self.cfg.raw("FINVIZ_FILTERS")
        columns = self.cfg.raw("FINVIZ_COLUMNS")
        per_screen = max(1, top_n)
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for name in names:
            signal = SIGNALS.get(name, name)
            if not intraday_signal_ok(signal):
                log.warning("finviz movers: %s ignores intraday timeframes; skipping", signal)
                continue
            try:
                rows = self.screener(filters=filters, signal=signal, columns=columns, ttl=ttl)
            except FinvizTokenRejected:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("finviz movers signal %s failed: %s", name, self.error_text(exc))
                continue
            added = 0
            for row in rows:
                ticker = str(row_get(row, "Ticker") or "").upper().strip()
                if not ticker or ticker in seen:
                    continue
                seen.add(ticker)
                out.append(row)
                added += 1
                if added >= per_screen:
                    break
        return out

    def snapshot(self, symbols: Iterable[str], session: Optional[str] = None,
                 ttl: float = 0.0) -> list[dict]:
        """Normalized quotes for specific symbols, built from the screener CSV."""
        session = session or session_for()
        syms = [str(s).upper().strip() for s in symbols if str(s).strip()]
        if not syms:
            return []
        batch = max(1, self.cfg.int("FINVIZ_QUOTE_BATCH", 40))
        columns = self.cfg.raw("FINVIZ_COLUMNS")
        quotes: list[dict] = []
        for i in range(0, len(syms), batch):
            chunk = syms[i:i + batch]
            try:
                rows = self.screener(tickers=",".join(chunk), filters="", columns=columns,
                                     ttl=ttl)
            except FinvizTokenRejected:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("finviz snapshot failed for %d symbols: %s", len(chunk),
                            self.error_text(exc))
                continue
            for row in rows:
                quote = self.normalize_row(row, session)
                if quote:
                    quotes.append(quote)
        return quotes

    def normalize_row(self, row: dict[str, str], session: str) -> Optional[dict]:
        """Turn one screener row into the system's quote shape (6.4).

        Finviz has no bid/ask, so those stay empty. The previous close comes from
        the Change percentage (or the `Prev Close` column). In pre/post the
        regular `Price`/`Change` stay frozen at the last regular close, so the
        dedicated After-Hours Close/Change columns are preferred when present.
        """
        symbol = str(row_get(row, "Ticker") or "").upper().strip()
        if not symbol:
            return None
        price = row_num(row, "Price")
        change_pct = row_num(row, "Change")
        # Only the post session needs the override: Finviz keeps Price/Change at
        # the regular close after 16:00, with the real move in After-Hours
        # Close/Change. In pre-market Finviz already folds the pre-market print
        # into Price/Change, and the After-Hours columns would be yesterday's.
        extended = session == "post"
        if extended and self.cfg.bool("FINVIZ_EXTENDED_HOURS", True):
            ah_price = row_num(row, "After-Hours Close")
            ah_change = row_num(row, "After-Hours Change")
            if ah_price and ah_price > 0:
                # Express the move from the regular close so pct_vs_close
                # includes the extended-hours move.
                base = price if (price and price > 0) else None
                if base is not None and ah_change is not None:
                    price = base * (1 + ah_change / 100.0)
                else:
                    price = ah_price
        if not price or price <= 0:
            return None
        prev_close = None
        if change_pct is not None and (1 + change_pct / 100.0) > 0:
            prev_close = price / (1 + change_pct / 100.0)
        if prev_close is None or extended:
            # The Change-derived value is wrong once an extended price is used,
            # and is unavailable when the Change column is blank.
            prev_close = row_num(row, "Prev Close", "Previous Close") or prev_close
        volume = row_num(row, "Volume") or 0.0
        avg_volume = avg_volume_shares(row)
        rel_volume = row_num(row, "Relative Volume", "Rel Volume")
        return {
            "symbol": symbol,
            "code": symbol,
            "ts": time.time(),
            "price": price,
            "prev_close": prev_close,
            "cum_volume": volume,
            "open_price": row_num(row, "Open"),
            "high_price": row_num(row, "High"),
            "low_price": row_num(row, "Low"),
            "bid": None,
            "ask": None,
            "market_cap": row_num(row, "Market Cap"),
            "float_shares": row_num(row, "Shares Float", "Float"),
            "avg_volume": avg_volume,
            "rel_volume": rel_volume,
            "sector": row_get(row, "Sector") or "",
            "change_pct": change_pct,
            "intraday_perf": {str(mins): row_num(row, col) for mins, col in PERF_COLUMNS
                              if row_num(row, col) is not None},
            "halted": False,
            "session": session,
            "source": "finviz",
        }

    def verify(self, ticker: str) -> Optional[dict[str, str]]:
        """Verification row for one alerted stock (6.7).

        Uses the configured view (default 152) so the custom column set applies: the
        Overview view (111) ignores `c=` and omits float, short float and average volume.
        """
        try:
            rows = self.screener(tickers=ticker, filters="",
                                 view=self.cfg.raw("FINVIZ_VIEW") or "152",
                                 columns=self.cfg.raw("FINVIZ_COLUMNS"), ttl=0)
        except FinvizTokenRejected:
            raise
        except SourceError as exc:
            log.warning("finviz verify failed for %s: %s", ticker, self.error_text(exc))
            return None
        for row in rows:
            if str(row.get("Ticker", "")).upper() == ticker.upper():
                return row
        return rows[0] if rows else None

    def stock_history(self, ticker: str, period: str = "d") -> list[dict[str, str]]:
        return self.fetch_csv("stock", t=ticker, p=period, ttl=3600.0)

    def groups(self, group: str = "sector", view: str = "152") -> list[dict[str, str]]:
        return self.fetch_csv("groups", g=group, v=view, ttl=3600.0)

    def latest_filings(self, ticker: str) -> list[dict[str, str]]:
        return self.fetch_csv("latest-filings", t=ticker, ty="lf", o="-filingDate", ttl=600.0)

    def news(self, view: str = "1") -> list[dict[str, str]]:
        return self.fetch_csv("news", v=view, ttl=300.0)

    def insiders(self, ticker: str, tc: str = "7") -> list[dict[str, str]]:
        return self.fetch_csv("insiders", t=ticker, tc=tc, ttl=1800.0)

    def economic_calendar(self, date_from: str) -> list[dict[str, str]]:
        return self.fetch_csv("calendar/economic", dateFrom=date_from, ttl=3600.0)

    # -- health -------------------------------------------------------------
    def health(self) -> tuple[bool, str]:
        try:
            rows = self.screener(tickers="AAPL", filters="", ttl=0)
            return True, f"screener ok ({len(rows)} row(s))"
        except FinvizTokenRejected as exc:
            return False, f"token rejected ({self.error_text(exc)})"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)


def row_get(row: dict[str, str], *names: str) -> Optional[str]:
    """Case-insensitive column lookup."""
    lowered = {str(k).lower(): v for k, v in row.items()}
    for n in names:
        if n.lower() in lowered:
            return lowered[n.lower()]
    return None


def row_num(row: dict[str, str], *names: str) -> Optional[float]:
    return fnum(row_get(row, *names))
