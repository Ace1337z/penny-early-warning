"""Shariah compliance screening: halal.sh + Musaffa, cached, shown on every alert (6.17)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .sources.base import BaseClient, SourceError, fnum

log = logging.getLogger(__name__)

COMPLIANT = "COMPLIANT"
DOUBTFUL = "DOUBTFUL"
NON_COMPLIANT = "NON-COMPLIANT"
NOT_SCREENED = "NOT SCREENED"
ERROR = "ERROR"
UNKNOWN = "UNKNOWN"

SCREENED = {COMPLIANT, DOUBTFUL, NON_COMPLIANT}

NOTE = "automated screening, not a religious ruling; confirm with your own scholar"
REFRESH_FORMS = ("10-K", "10-Q", "8-K")


def combine(statuses: list[str]) -> str:
    """Combine per-source statuses exactly as in 6.17."""
    results = [s for s in statuses if s in SCREENED]
    if results:
        if all(s == COMPLIANT for s in results):
            return COMPLIANT
        if any(s == NON_COMPLIANT for s in results):
            return NON_COMPLIANT
        if any(s == DOUBTFUL for s in results):
            return DOUBTFUL
    return UNKNOWN


@dataclass
class SourceResult:
    source: str
    status: str = NOT_SCREENED
    as_of: str = ""
    fetched_at: float = 0.0
    detail: dict = field(default_factory=dict)
    stale: bool = False

    def as_dict(self) -> dict:
        return {"source": self.source, "status": self.status, "as_of": self.as_of,
                "detail": self.detail, "stale": self.stale}

    def short(self) -> str:
        bits = [f"{self.source}: {self.status}"]
        if self.as_of:
            bits.append(f"({self.as_of})")
        if self.stale:
            bits.append("[stale]")
        return " ".join(bits)

    def detail_text(self) -> str:
        d = self.detail or {}
        parts = []
        for key, label in (("business_activity", "business"), ("debt_ratio", "debt"),
                           ("interest_income_ratio", "interest income"),
                           ("purification", "purification"), ("rating", "rating")):
            if d.get(key) not in (None, ""):
                parts.append(f"{label} {d[key]}")
        if d.get("reasons"):
            reasons = d["reasons"]
            if isinstance(reasons, list):
                reasons = "; ".join(str(r) for r in reasons[:3])
            parts.append(str(reasons)[:200])
        return f"{self.source}: {self.status}" + (f" ({self.as_of})" if self.as_of else "") + \
               (" - " + ", ".join(parts) if parts else "")


# --- sources ----------------------------------------------------------------

class ShariahSource:
    name = "source"

    def lookup(self, symbol: str) -> SourceResult:
        raise NotImplementedError

    def health(self) -> tuple[bool, str]:
        return False, "not configured"


class HalalShSource(BaseClient, ShariahSource):
    name = "halal.sh"

    @property
    def configured(self) -> bool:
        return bool(self.cfg.raw("HALALSH_API_KEY"))

    def lookup(self, symbol: str) -> SourceResult:
        if not self.configured:
            return SourceResult(self.name, NOT_SCREENED)
        base = str(self.cfg.raw("HALALSH_BASE_URL") or "https://halal.sh").rstrip("/")
        url = f"{base}/api/v1/screen/{symbol.upper()}"
        try:
            data = self.get_json(url, headers={"Authorization": f"Bearer {self.cfg.raw('HALALSH_API_KEY')}"},
                                 params={"symbol": symbol.upper()})
        except Exception as exc:  # noqa: BLE001
            log.debug("halal.sh failed for %s: %s", symbol, self.error_text(exc))
            return SourceResult(self.name, ERROR, detail={"error": self.error_text(exc)})
        return self._parse(symbol, data)

    def _parse(self, symbol: str, data: Any) -> SourceResult:
        if not isinstance(data, dict):
            return SourceResult(self.name, ERROR, detail={"error": "unexpected response"})
        raw = str(data.get("status") or data.get("ruling") or data.get("result") or "").lower()
        status = NOT_SCREENED
        if any(k in raw for k in ("non-compliant", "noncompliant", "not compliant", "haram")):
            status = NON_COMPLIANT
        elif "doubt" in raw or "questionable" in raw:
            status = DOUBTFUL
        elif "compliant" in raw or raw in ("halal", "pass"):
            status = COMPLIANT
        detail = {
            "business_activity": data.get("business_activity") or data.get("activity"),
            "debt_ratio": data.get("debt_ratio") or data.get("debt_to_assets"),
            "interest_income_ratio": data.get("interest_income_ratio"),
            "purification": data.get("purification") or data.get("purification_percent"),
            "reasons": data.get("reasons") or data.get("citations"),
            "standard": data.get("standard") or "AAOIFI 21",
        }
        return SourceResult(self.name, status, as_of=str(data.get("as_of") or data.get("date") or ""),
                            detail={k: v for k, v in detail.items() if v not in (None, "")})

    def health(self) -> tuple[bool, str]:
        if not self.configured:
            return False, "not configured (optional)"
        result = self.lookup("AAPL")
        if result.status == ERROR:
            return False, result.detail.get("error", "error")
        return True, f"reachable (AAPL: {result.status})"


class MusaffaSource(BaseClient, ShariahSource):
    name = "Musaffa"

    @property
    def configured(self) -> bool:
        return bool(self.cfg.raw("MUSAFFA_API_KEY"))

    def lookup(self, symbol: str) -> SourceResult:
        if not self.configured:
            return SourceResult(self.name, NOT_SCREENED)
        base = str(self.cfg.raw("MUSAFFA_BASE_URL") or "https://musaffa.com").rstrip("/")
        url = f"{base}/api/v1/screening"
        try:
            data = self.get_json(url, headers={"X-API-KEY": self.cfg.raw("MUSAFFA_API_KEY"),
                                               "Authorization": f"Bearer {self.cfg.raw('MUSAFFA_API_KEY')}"},
                                 params={"symbol": symbol.upper()})
        except Exception as exc:  # noqa: BLE001
            log.debug("musaffa failed for %s: %s", symbol, self.error_text(exc))
            return SourceResult(self.name, ERROR, detail={"error": self.error_text(exc)})
        return self._parse(data)

    def _parse(self, data: Any) -> SourceResult:
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return SourceResult(self.name, ERROR, detail={"error": "unexpected response"})
        raw = str(data.get("status") or data.get("compliance") or data.get("result") or "").lower()
        status = NOT_SCREENED
        if any(k in raw for k in ("not halal", "non-compliant", "noncompliant", "haram")):
            status = NON_COMPLIANT
        elif "doubt" in raw:
            status = DOUBTFUL
        elif "halal" in raw or "compliant" in raw:
            status = COMPLIANT
        detail = {
            "rating": data.get("rating"),
            "business_activity": data.get("business_activity") or data.get("activity"),
            "debt_ratio": data.get("debt_ratio"),
            "interest_income_ratio": data.get("interest_income_ratio"),
            "purification": data.get("purification"),
            "reasons": data.get("reasons"),
        }
        return SourceResult(self.name, status,
                            as_of=str(data.get("as_of") or data.get("screening_date") or ""),
                            detail={k: v for k, v in detail.items() if v not in (None, "")})

    def health(self) -> tuple[bool, str]:
        if not self.configured:
            return False, "not configured (optional)"
        result = self.lookup("AAPL")
        if result.status == ERROR:
            return False, result.detail.get("error", "error")
        return True, f"reachable (AAPL: {result.status})"


class FakeShariahSource(ShariahSource):
    """Deterministic source for tests (10.10)."""

    def __init__(self, name: str, mapping: dict[str, str],
                 details: Optional[dict[str, dict]] = None, error_symbols: Iterable[str] = ()):
        self.name = name
        self.mapping = mapping
        self.details = details or {}
        self.error_symbols = set(error_symbols)
        self.calls: list[str] = []

    def lookup(self, symbol: str) -> SourceResult:
        self.calls.append(symbol)
        if symbol.upper() in self.error_symbols:
            return SourceResult(self.name, ERROR, detail={"error": "fake outage"})
        status = self.mapping.get(symbol.upper(), NOT_SCREENED)
        return SourceResult(self.name, status, as_of="2026-01-01",
                            detail=self.details.get(symbol.upper(), {}))

    def health(self) -> tuple[bool, str]:
        return True, "fake shariah source"


# --- display ----------------------------------------------------------------

def one_line(status: dict) -> str:
    combined = status.get("combined", UNKNOWN)
    sources = status.get("sources", {})
    active = [s for s in sources.values() if s.status in SCREENED]
    names = ", ".join(s.source for s in active)
    if not active and not sources:
        return "Shariah: not checked"
    if combined == UNKNOWN:
        return "Shariah: UNKNOWN (no screening result)"
    return f"Shariah: {combined}" + (f" ({names})" if names else "")


def detail_lines(status: dict, sources: list["ShariahSource"]) -> list[str]:
    lines = [f"Shariah: {status.get('combined', UNKNOWN)}"]
    for source in sources:
        r = status.get("sources", {}).get(source.name)
        if r is None:
            lines.append(f"  {source.name}: not checked")
        else:
            lines.append("  " + r.detail_text() + (" [stale cache]" if r.stale else ""))
    lines.append(f"  ({NOTE})")
    return lines


# --- service ----------------------------------------------------------------

class ShariahService:
    def __init__(self, cfg, db, sources: list[ShariahSource], telegram=None):
        self.cfg = cfg
        self.db = db
        self.sources = sources
        self.telegram = telegram
        self._notified_month = ""
        self._calls: dict[str, int] = {}

    @property
    def mode(self) -> str:
        return str(self.cfg.raw("SHARIAH_MODE") or "tag").strip().lower()

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and any(
            getattr(s, "configured", True) for s in self.sources)

    def _ttl_seconds(self) -> float:
        return self.cfg.float("SHARIAH_TTL_DAYS", 7) * 86400

    def cached(self, symbol: str) -> dict[str, SourceResult]:
        rows = self.db.query("SELECT * FROM compliance WHERE symbol=?", (symbol.upper(),))
        out = {}
        for row in rows:
            try:
                detail = json.loads(row["detail"]) if row["detail"] else {}
            except (json.JSONDecodeError, TypeError):
                detail = {}
            out[row["source"]] = SourceResult(
                source=row["source"], status=row["status"] or NOT_SCREENED,
                as_of=row["as_of"] or "", fetched_at=row["fetched_at"] or 0.0, detail=detail)
        return out

    def _store(self, symbol: str, result: SourceResult) -> None:
        self.db.execute(
            "INSERT INTO compliance(symbol, source, status, as_of, fetched_at, detail) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(symbol, source) DO UPDATE SET "
            "status=excluded.status, as_of=excluded.as_of, fetched_at=excluded.fetched_at, "
            "detail=excluded.detail",
            (symbol.upper(), result.source, result.status, result.as_of, result.fetched_at,
             json.dumps(result.detail)))

    def _count_call(self, source: str) -> None:
        self._calls[source] = self._calls.get(source, 0) + 1
        limit = self.cfg.int("SHARIAH_MAX_CALLS_MONTH", 0)
        if limit <= 0:
            return
        month = time.strftime("%Y-%m")
        if self._calls[source] >= 0.9 * limit and self._notified_month != month:
            self._notified_month = month
            if self.telegram:
                try:
                    self.telegram.send(
                        f"Shariah source {source}: {self._calls[source]} calls this month, "
                        f"at 90% of your limit ({limit}).")
                except Exception as exc:  # noqa: BLE001
                    log.debug("shariah quota notice failed: %s", exc)

    def status(self, symbol: str, filings: Optional[list[dict]] = None,
               fetch: bool = True) -> dict:
        """Combined status plus each source's result. Never raises; never delays an alert."""
        symbol = symbol.upper()
        cached = self.cached(symbol)
        ttl = self._ttl_seconds()
        now = time.time()
        need_refresh = self._new_financial_filing(cached, filings)

        results: list[SourceResult] = []
        for source in self.sources:
            if not getattr(source, "configured", True):
                results.append(SourceResult(source.name, NOT_SCREENED))
                continue
            entry = cached.get(source.name)
            fresh = entry is not None and (now - entry.fetched_at) < ttl and not need_refresh
            if fresh and fetch is not False:
                results.append(entry)
                continue
            if not fetch:
                if entry is not None:
                    entry.stale = True
                    results.append(entry)
                else:
                    results.append(SourceResult(source.name, NOT_SCREENED))
                continue
            self._count_call(source.name)
            try:
                result = source.lookup(symbol)
            except Exception as exc:  # noqa: BLE001
                result = SourceResult(source.name, ERROR, detail={"error": str(exc)})
            result.fetched_at = now
            if result.status in (ERROR, NOT_SCREENED) and entry is not None:
                # A source outage uses the stale cached result, marked with its date.
                entry.stale = True
                results.append(entry)
                continue
            self._store(symbol, result)
            results.append(result)

        combined = combine([r.status for r in results])
        return {
            "symbol": symbol,
            "combined": combined,
            "sources": {r.source: r for r in results},
            "note": NOTE,
        }

    @staticmethod
    def _new_financial_filing(cached: dict[str, SourceResult],
                              filings: Optional[list[dict]]) -> bool:
        if not filings:
            return False
        for filing in filings:
            form = str(filing.get("form") or "").upper()
            items = str(filing.get("items") or "")
            if form in ("10-K", "10-Q") or (form.startswith("8-K") and "2.02" in items):
                date = str(filing.get("date") or "")
                if not date:
                    continue
                if all(not r.as_of or r.as_of < date for r in cached.values()):
                    return True
        return False

    # -- display ------------------------------------------------------------
    def one_line(self, status: dict) -> str:
        return one_line(status)

    def detail_lines(self, status: dict) -> list[str]:
        return detail_lines(status, self.sources)
    def should_alert(self, status: dict) -> tuple[bool, str]:
        """Mode handling: tag, only_compliant, hide_noncompliant, off (6.17)."""
        mode = self.mode
        combined = status.get("combined", UNKNOWN)
        if mode in ("off", "tag"):
            return True, ""
        if mode == "only_compliant":
            if combined == COMPLIANT:
                return True, ""
            return False, f"suppressed ({combined})"
        if mode == "hide_noncompliant":
            if combined == NON_COMPLIANT:
                return False, "suppressed (NON-COMPLIANT)"
            return True, ""
        return True, ""

    def health(self) -> list[tuple[str, bool, str]]:
        out = []
        for source in self.sources:
            ok, msg = source.health()
            out.append((source.name, ok, msg))
        return out
