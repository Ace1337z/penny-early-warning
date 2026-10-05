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

log = logging.getLogger(__name__)

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
        self.limiter = RateLimiter(rate=max(1, cfg.int("FINVIZ_MAX_PER_MIN", 20)), per_seconds=60.0)
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
    def screener(self, *, tickers: str = "", filters: str = "", view: str = "",
                 columns: str = "", ttl: float = 0.0) -> list[dict[str, str]]:
        params = {
            "v": view or self.cfg.raw("FINVIZ_VIEW") or "111",
            "f": filters or self.cfg.raw("FINVIZ_FILTERS") or "",
        }
        if tickers:
            params["t"] = tickers
        if columns:
            params["c"] = columns
        return self.fetch_csv("screener", ttl=ttl, **params)

    def universe(self, ttl: float = 300.0) -> list[dict[str, str]]:
        """Daily universe rows (6.2)."""
        return self.screener(filters=self.cfg.raw("FINVIZ_FILTERS"),
                             columns=self.cfg.raw("FINVIZ_COLUMNS"),
                             ttl=ttl)

    def verify(self, ticker: str) -> Optional[dict[str, str]]:
        """Verification row for one alerted stock (6.7)."""
        try:
            rows = self.screener(tickers=ticker, filters="", view="111",
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
