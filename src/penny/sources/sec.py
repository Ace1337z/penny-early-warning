"""SEC EDGAR: ticker -> CIK map and submissions (filings). Every request needs a UA (5.3)."""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

from .base import BaseClient, SourceError

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

INTERESTING_PREFIXES = (
    "8-K", "S-1", "S-3", "424B", "F-1", "F-3", "13D", "13G", "SCHEDULE 13", "SC 13",
    "4", "6-K", "NT 10", "25",
)


class SecClient(BaseClient):
    name = "sec"

    def __init__(self, cfg, secrets: Optional[Iterable[str]] = None):
        super().__init__(cfg, secrets)
        self.user_agent = str(cfg.raw("SEC_USER_AGENT") or "").strip()
        self._cik_map: dict[str, int] = {}
        self._cik_loaded = 0.0

    def _headers(self) -> dict[str, str]:
        ua = self.user_agent or "penny-early-warning (set SEC_USER_AGENT)"
        return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}

    def cik_for(self, ticker: str) -> Optional[int]:
        """Ticker -> CIK, cached 7 days."""
        if not self._cik_map or time.time() - self._cik_loaded > 7 * 86400:
            data = self.get_json(TICKERS_URL, headers=self._headers())
            mapping = {}
            if isinstance(data, dict):
                for item in data.values():
                    try:
                        mapping[str(item["ticker"]).upper()] = int(item["cik_str"])
                    except (KeyError, TypeError, ValueError):
                        continue
            self._cik_map = mapping
            self._cik_loaded = time.time()
        return self._cik_map.get(ticker.upper())

    def filings(self, ticker: str, days: int = 5) -> list[dict]:
        cik = self.cik_for(ticker)
        if not cik:
            raise SourceError(f"sec: no CIK for {ticker}")
        url = SUBMISSIONS_URL.format(cik=cik)
        data = self.get_json(url, headers=self._headers())
        recent = (data.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        accs = recent.get("accessionNumber") or []
        items = recent.get("items") or []
        primary = recent.get("primaryDocument") or []

        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        out = []
        for i, form in enumerate(forms):
            fdate = dates[i] if i < len(dates) else ""
            if fdate < cutoff:
                continue
            if not str(form).upper().startswith(INTERESTING_PREFIXES):
                continue
            acc = (accs[i] if i < len(accs) else "").replace("-", "")
            doc = primary[i] if i < len(primary) else ""
            url_doc = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}" if acc and doc else ""
            out.append({
                "form": form,
                "date": fdate,
                "items": items[i] if i < len(items) else "",
                "url": url_doc,
                "source": "sec",
            })
        out.sort(key=lambda x: x["date"], reverse=True)
        return out

    def health(self) -> tuple[bool, str]:
        if not self.user_agent:
            return False, "SEC_USER_AGENT not set (required)"
        try:
            cik = self.cik_for("AAPL")
            return (True, f"ok (AAPL CIK {cik})") if cik else (False, "AAPL not found in ticker map")
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
