"""Market context: S&P 500, Nasdaq, small caps, sectors, economic events, news (6.10)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .util import safe_div
from .sources.finviz import FinvizTokenRejected, row_get, row_num

log = logging.getLogger(__name__)

QUOTE_TTL = 30.0
SECTOR_TTL = 3600.0
EVENT_TTL = 3600.0
NEWS_TTL = 300.0


@dataclass
class MarketContext:
    quotes: dict[str, dict] = field(default_factory=dict)
    index_levels: dict[str, dict] = field(default_factory=dict)
    sectors: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    news: list[str] = field(default_factory=list)
    regime: str = "unknown"
    fetched_at: float = 0.0
    missing: list[str] = field(default_factory=list)

    def as_prompt_dict(self) -> dict:
        return {
            "indices": {k: v for k, v in self.quotes.items()},
            "levels": {k: v for k, v in self.index_levels.items()},
            "sectors": self.sectors[:6],
            "events": self.events[:5],
            "news": self.news[:5],
            "regime": self.regime,
        }

    def headline(self) -> str:
        bits = []
        for sym, q in self.quotes.items():
            if q.get("change_pct") is not None:
                bits.append(f"{sym} {q['change_pct']:+.2f}%")
        for sym, lv in self.index_levels.items():
            if lv.get("change_pct") is not None and sym not in self.quotes:
                bits.append(f"{sym} {lv['change_pct']:+.2f}%")
        return "market: " + (", ".join(bits) if bits else "no data") + f" | regime {self.regime}"


class MarketContextProvider:
    """Shared, cached market context used by every AI request and message."""

    def __init__(self, cfg, finviz=None, yahoo=None):
        self.cfg = cfg
        self.finviz = finviz
        self.yahoo = yahoo
        self._context = MarketContext()
        self._quotes_ts = 0.0
        self._sectors_ts = 0.0
        self._events_ts = 0.0
        self._news_ts = 0.0
        self._samples: dict[str, list[tuple[float, float]]] = {}
        self._last_error = ""

    # -- samples ------------------------------------------------------------
    def _track(self, symbol: str, ts: float, price: float) -> None:
        s = self._samples.setdefault(symbol, [])
        if s and ts - s[-1][0] < 55:
            s[-1] = (ts, price)
        else:
            s.append((ts, price))
        cutoff = ts - 6 * 3600
        self._samples[symbol] = [x for x in s if x[0] >= cutoff]

    def _change(self, symbol: str, minutes: int) -> Optional[float]:
        s = self._samples.get(symbol) or []
        if not s:
            return None
        target = s[-1][0] - minutes * 60
        past = [x for x in s if x[0] <= target]
        if not past:
            return None
        base = past[-1][1]
        if not base:
            return None
        return (s[-1][1] / base - 1.0) * 100.0

    # -- refresh ------------------------------------------------------------
    def get(self, force: bool = False) -> MarketContext:
        now = time.time()
        ctx = self._context
        ctx.missing = []

        if force or now - self._quotes_ts > QUOTE_TTL:
            self._refresh_quotes(ctx)
            self._quotes_ts = now
        if force or now - self._sectors_ts > SECTOR_TTL:
            self._refresh_sectors(ctx)
            self._sectors_ts = now
        if force or now - self._events_ts > EVENT_TTL:
            self._refresh_events(ctx)
            self._events_ts = now
        if force or now - self._news_ts > NEWS_TTL:
            self._refresh_news(ctx)
            self._news_ts = now

        ctx.regime = self._regime(ctx)
        ctx.fetched_at = now
        return ctx

    def _refresh_quotes(self, ctx: MarketContext) -> None:
        symbols = self.cfg.list("MARKET_SYMBOLS") or ["SPY", "QQQ", "IWM"]
        ctx.quotes = {}
        if self.finviz:
            try:
                quotes = self.finviz.snapshot(symbols)
                for q in quotes:
                    sym = q.get("symbol")
                    if not sym or not q.get("price"):
                        continue
                    change = None
                    if q.get("prev_close"):
                        change = (q["price"] / q["prev_close"] - 1.0) * 100.0
                    self._track(sym, q["ts"], q["price"])
                    ctx.quotes[sym] = {
                        "price": round(q["price"], 4),
                        "change_pct": round(change, 2) if change is not None else None,
                        "change_15m": self._change(sym, 15),
                        "change_60m": self._change(sym, 60),
                    }
            except Exception as exc:  # noqa: BLE001
                log.debug("market index snapshot failed: %s", exc)
                ctx.missing.append("index quotes (finviz)")
        if not ctx.quotes and self.yahoo:
            try:
                levels = self.yahoo.index_levels(("^GSPC", "^IXIC", "^VIX"))
                ctx.index_levels = levels
            except Exception as exc:  # noqa: BLE001
                log.debug("market index levels failed: %s", exc)
                ctx.missing.append("index levels (yahoo)")
        elif self.yahoo:
            try:
                ctx.index_levels = self.yahoo.index_levels(("^GSPC", "^IXIC", "^VIX"))
            except Exception:  # noqa: BLE001
                pass

    def _refresh_sectors(self, ctx: MarketContext) -> None:
        if not self.finviz:
            return
        try:
            rows = self.finviz.groups("sector", "152")
        except FinvizTokenRejected:
            ctx.missing.append("sectors (finviz token)")
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("sector fetch failed: %s", exc)
            ctx.missing.append("sectors")
            return
        sectors = []
        for row in rows:
            name = row_get(row, "Name", "Sector") or ""
            change = row_num(row, "Change", "Perf")
            if name:
                sectors.append({"name": name, "change_pct": change})
        sectors.sort(key=lambda s: (s["change_pct"] is None, -(s["change_pct"] or 0)))
        ctx.sectors = sectors

    def _refresh_events(self, ctx: MarketContext) -> None:
        if not self.finviz:
            return
        today = time.strftime("%Y-%m-%d")
        try:
            rows = self.finviz.economic_calendar(today)
        except FinvizTokenRejected:
            ctx.missing.append("economic calendar (finviz token)")
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("economic calendar failed: %s", exc)
            ctx.missing.append("economic calendar")
            return
        events = []
        for row in rows[:12]:
            events.append({
                "time": row_get(row, "Time", "Date") or "",
                "event": row_get(row, "Event") or "",
                "impact": row_get(row, "Impact") or "",
                "actual": row_get(row, "Actual") or "",
                "forecast": row_get(row, "Forecast") or "",
            })
        ctx.events = events

    def _refresh_news(self, ctx: MarketContext) -> None:
        if not self.finviz:
            return
        try:
            rows = self.finviz.news("1")
        except FinvizTokenRejected:
            ctx.missing.append("market news (finviz token)")
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("market news failed: %s", exc)
            ctx.missing.append("market news")
            return
        headlines = []
        for row in rows[:5]:
            title = row_get(row, "Title", "Headline") or ""
            if title:
                headlines.append(title[:180])
        ctx.news = headlines

    @staticmethod
    def _regime(ctx: MarketContext) -> str:
        def chg(sym: str) -> Optional[float]:
            q = ctx.quotes.get(sym)
            if q and q.get("change_pct") is not None:
                return q["change_pct"]
            return None

        spy, qqq, iwm = chg("SPY"), chg("QQQ"), chg("IWM")
        if spy is None or qqq is None:
            return "unknown"
        if spy > 0 and qqq > 0 and (iwm is None or iwm > 0):
            return "risk-on"
        if spy < -0.5 and qqq < -0.5:
            return "risk-off"
        return "mixed"

    def sector_rank(self, sector: Optional[str]) -> Optional[int]:
        if not sector:
            return None
        for i, s in enumerate(self._context.sectors, start=1):
            if s["name"].lower() == str(sector).lower():
                return i
        return None
