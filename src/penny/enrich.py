"""Enrichment: candles, news, filings, short data, insiders, verification (6.7, 6.8)."""

from __future__ import annotations

import concurrent.futures as futures
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .fib import FibLevels, TradePlan, fib_levels, trade_plan
from .sources.finviz import FinvizTokenRejected, avg_volume_shares, row_get, row_num
from .technicals import Technicals, analyze
from .util import ET, age_str, safe_div

log = logging.getLogger(__name__)

FILING_PREFIXES = ("8-K", "S-1", "S-3", "424B", "F-1", "F-3", "13D", "13G",
                   "SCHEDULE 13", "SC 13", "4", "6-K", "NT 10", "25")


@dataclass
class Enrichment:
    symbol: str
    bars_1m: list[dict] = field(default_factory=list)
    news: list[dict] = field(default_factory=list)
    outlets: int = 0
    filings: list[dict] = field(default_factory=list)
    short_interest: list[dict] = field(default_factory=list)
    short_volume: Optional[dict] = None
    float_shares: Optional[float] = None
    market_cap: Optional[float] = None
    insiders: list[dict] = field(default_factory=list)
    finviz_row: Optional[dict] = None
    verification: dict = field(default_factory=dict)
    sector: str = ""
    missing: list[str] = field(default_factory=list)
    technicals: Optional[Technicals] = None
    fib: Optional[FibLevels] = None
    plan: Optional[TradePlan] = None
    errors: list[str] = field(default_factory=list)

    def news_key(self) -> str:
        """A stable key so a new headline invalidates the AI cache (6.11 rule 2)."""
        if not self.news:
            return ""
        return "|".join(str(n.get("title", ""))[:60].lower() for n in self.news[:3])

    def fresh_short(self) -> Optional[dict]:
        values = [s for s in self.short_interest if s.get("shares_short") or s.get("ratio")]
        if not values:
            return None
        return max(values, key=lambda s: str(s.get("date") or ""))


class Enricher:
    """Runs the per-alert workers in parallel; each source fails soft (6.8)."""

    def __init__(self, cfg, *, finviz=None, alpaca=None, finnhub=None,
                 sec=None, finra=None, yahoo=None, market=None):
        self.cfg = cfg
        self.finviz = finviz
        self.alpaca = alpaca
        self.finnhub = finnhub
        self.sec = sec
        self.finra = finra
        self.yahoo = yahoo
        self.market = market

    # -- public -------------------------------------------------------------
    def enrich(self, symbol: str, quote: dict, metrics=None) -> Enrichment:
        enr = Enrichment(symbol=symbol)
        enr.float_shares = quote.get("float_shares")
        enr.market_cap = quote.get("market_cap")

        # One Finviz verification fetch, shared by the short-interest and the
        # verification jobs. Fetching it twice (uncached) was two rate-limited
        # requests for the same row on every check.
        with futures.ThreadPoolExecutor(
                max_workers=min(6, max(1, self.cfg.int("WORKERS", 8)))) as pool:
            verify_fut = pool.submit(self._verify, symbol, quote)

            jobs = {
                "candles": lambda: self._candles(symbol),
                "news": lambda: self._news(symbol),
                "filings": lambda: self._filings(symbol),
                "short": lambda: self._short(symbol, verify_fut),
                "insiders": lambda: self._insiders(symbol),
                "finviz": lambda: verify_fut.result(),
            }
            results: dict[str, object] = {}
            futs = {pool.submit(fn): name for name, fn in jobs.items()}
            for fut in futures.as_completed(futs):
                name = futs[fut]
                try:
                    results[name] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    results[name] = exc
                    log.debug("enrichment %s failed for %s: %s", name, symbol, exc)

        if isinstance(results.get("candles"), list):
            enr.bars_1m = results["candles"]
        else:
            enr.missing.append("candles")

        if isinstance(results.get("news"), tuple):
            enr.news, enr.outlets = results["news"]
        else:
            enr.missing.append("news")

        if isinstance(results.get("filings"), list):
            enr.filings = results["filings"]
        else:
            enr.missing.append("filings")

        if isinstance(results.get("short"), dict):
            enr.short_interest = results["short"].get("interest") or []
            enr.short_volume = results["short"].get("volume")
        else:
            enr.missing.append("short data")

        if isinstance(results.get("insiders"), list):
            enr.insiders = results["insiders"]

        if isinstance(results.get("finviz"), dict):
            enr.finviz_row = results["finviz"].get("row")
            enr.verification = results["finviz"].get("verification") or {}
            if results["finviz"].get("float_shares"):
                enr.float_shares = enr.float_shares or results["finviz"]["float_shares"]
            if results["finviz"].get("market_cap"):
                enr.market_cap = enr.market_cap or results["finviz"]["market_cap"]
            enr.sector = results["finviz"].get("sector") or ""
        else:
            enr.missing.append("finviz verification")

        # Technicals, Fibonacci and the trade plan from the candles.
        if enr.bars_1m:
            price = quote.get("price")
            enr.technicals = analyze(enr.bars_1m, price)
            if enr.technicals.swing_low and enr.technicals.swing_high and price:
                enr.fib = fib_levels(enr.technicals.swing_low, enr.technicals.swing_high)
                enr.plan = trade_plan(price, enr.fib, self.cfg.float("RISK_USD", 50))
        return enr

    # -- workers ------------------------------------------------------------
    def _candles(self, symbol: str) -> list[dict]:
        """1-minute bars including pre/post: Alpaca -> Yahoo (6.8).

        Finviz is the live feed and has no intraday candle endpoint, so candles
        rely on the optional Alpaca and Yahoo sources; the plan degrades softly
        when neither is configured.
        """
        now = datetime.now(tz=ET)
        start = now.replace(hour=4, minute=0, second=0, microsecond=0) - timedelta(days=1)
        if self.alpaca and getattr(self.alpaca, "configured", False):
            try:
                bars = self.alpaca.bars(symbol, start, now)
                if bars:
                    return bars
            except Exception as exc:  # noqa: BLE001
                log.debug("alpaca candles failed for %s: %s", symbol, exc)
        if self.yahoo:
            try:
                return self.yahoo.bars(symbol, days=2)
            except Exception as exc:  # noqa: BLE001
                log.debug("yahoo candles failed for %s: %s", symbol, exc)
        return []

    def _news(self, symbol: str) -> tuple[list[dict], int]:
        """Merge several sources, de-duplicate, newest first, up to 8 items (6.8)."""
        items: list[dict] = []

        def add(source_items, name):
            for item in source_items or []:
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                items.append({
                    "title": title,
                    "source": item.get("source") or name,
                    "url": item.get("url") or "",
                    "published": item.get("published") or "",
                    "summary": (item.get("summary") or "")[:200],
                })

        if self.alpaca and getattr(self.alpaca, "configured", False):
            try:
                add(self.alpaca.news([symbol]), "alpaca")
            except Exception as exc:  # noqa: BLE001
                log.debug("alpaca news failed: %s", exc)
        if self.finnhub and getattr(self.finnhub, "configured", False):
            try:
                add(self.finnhub.company_news(symbol), "finnhub")
            except Exception as exc:  # noqa: BLE001
                log.debug("finnhub news failed: %s", exc)
        if self.finviz:
            try:
                rows = self.finviz.news("1")
                for row in rows[:20]:
                    title = row_get(row, "Title", "Headline") or ""
                    if symbol.upper() in title.upper() or len(items) < 3:
                        add([{"title": title, "source": "finviz",
                              "published": row_get(row, "Date") or ""}], "finviz")
            except FinvizTokenRejected:
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("finviz news failed: %s", exc)
        if self.yahoo:
            try:
                add(self.yahoo.google_news(symbol), "google-news")
            except Exception as exc:  # noqa: BLE001
                log.debug("google news failed: %s", exc)

        seen: set[str] = set()
        merged: list[dict] = []
        for item in items:
            key = item["title"][:60].lower()
            if key in seen:
                continue
            seen.add(key)
            item["age"] = self._age_of(item.get("published"))
            merged.append(item)

        merged.sort(key=lambda i: (i.get("published") == "", str(i.get("published")), ), reverse=False)
        merged.sort(key=lambda i: str(i.get("published") or ""), reverse=True)
        outlets = len({i["source"] for i in merged})
        return merged[:8], outlets

    @staticmethod
    def _age_of(published) -> str:
        if not published:
            return ""
        text = str(published)
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %Z"):
            try:
                dt = datetime.strptime(text[:len(fmt) + 6], fmt).replace(tzinfo=ET)
                return age_str(time.time() - dt.timestamp())
            except ValueError:
                continue
        if text.isdigit():
            ts = int(text)
            if ts > 10_000_000_000:
                ts //= 1000
            return age_str(time.time() - ts)
        return ""

    def _filings(self, symbol: str) -> list[dict]:
        out: list[dict] = []
        if self.sec:
            try:
                out.extend(self.sec.filings(symbol, days=5))
            except Exception as exc:  # noqa: BLE001
                log.debug("sec filings failed for %s: %s", symbol, exc)
        if self.finviz:
            try:
                rows = self.finviz.latest_filings(symbol)
                cutoff = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
                for row in rows:
                    form = row_get(row, "Form", "Type") or ""
                    date = row_get(row, "Date", "Filing Date") or ""
                    if date and date < cutoff:
                        continue
                    if not str(form).upper().startswith(FILING_PREFIXES):
                        continue
                    out.append({"form": form, "date": date,
                                "items": row_get(row, "Item", "Items") or "",
                                "url": row_get(row, "URL", "Link") or "", "source": "finviz"})
            except FinvizTokenRejected:
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("finviz filings failed for %s: %s", symbol, exc)
        dedup: dict[tuple, dict] = {}
        for f in out:
            dedup[(str(f.get("form")), str(f.get("date")))] = f
        return sorted(dedup.values(), key=lambda f: str(f.get("date")), reverse=True)[:10]

    def _short(self, symbol: str, verify_fut=None) -> dict:
        interest: list[dict] = []
        volume: Optional[dict] = None

        if self.finviz:
            try:
                row = None
                if verify_fut is not None:
                    # Reuse the verification row instead of a second Finviz call.
                    row = (verify_fut.result() or {}).get("row")
                else:
                    row = self.finviz.verify(symbol)
                if row:
                    short_float = row_num(row, "Short Float", "ShortFloat")
                    if short_float is not None:
                        interest.append({"short_float_pct": short_float, "source": "finviz",
                                         "date": row_get(row, "Date") or ""})
            except FinvizTokenRejected:
                pass
            except Exception:  # noqa: BLE001
                pass

        if self.finra:
            try:
                data = self.finra.short_ratio(symbol)
                if data:
                    volume = volume or data
                    interest.append({"short_volume_ratio": data.get("ratio"),
                                     "date": data.get("date"), "source": "finra"})
            except Exception:  # noqa: BLE001
                pass

        return {"interest": interest, "volume": volume}

    def _insiders(self, symbol: str) -> list[dict]:
        if not self.finviz:
            return []
        try:
            rows = self.finviz.insiders(symbol)
        except FinvizTokenRejected:
            return []
        except Exception:  # noqa: BLE001
            return []
        out = []
        for row in rows[:5]:
            out.append({
                "owner": row_get(row, "Owner") or "",
                "relationship": row_get(row, "Relationship") or "",
                "date": row_get(row, "Date") or "",
                "transaction": row_get(row, "Transaction") or "",
                "cost": row_get(row, "Cost") or "",
                "value": row_get(row, "Value") or "",
            })
        return out

    def _verify(self, symbol: str, quote: dict) -> dict:
        """Finviz verification: compare price and volume, flag a mismatch (6.7)."""
        if not self.finviz:
            return {}
        try:
            row = self.finviz.verify(symbol)
        except FinvizTokenRejected:
            return {"verification": {"status": "token_rejected"}}
        except Exception as exc:  # noqa: BLE001
            log.debug("finviz verify failed for %s: %s", symbol, exc)
            return {"verification": {"status": "unverified", "note": str(exc)}}
        if not row:
            return {"verification": {"status": "unverified"}}

        f_price = row_num(row, "Price")
        f_change = row_num(row, "Change")
        f_volume = row_num(row, "Volume")
        m_price = quote.get("price")
        m_volume = quote.get("cum_volume")
        mismatch = []
        if f_price and m_price and abs(f_price / m_price - 1.0) > 0.03:
            mismatch.append(f"price feed {m_price} vs Finviz {f_price}")
        if f_volume and m_volume and abs(f_volume / m_volume - 1.0) > 0.25:
            mismatch.append(f"volume feed {m_volume:.0f} vs Finviz {f_volume:.0f}")
        return {
            "row": row,
            "float_shares": row_num(row, "Shares Float", "Float"),
            "market_cap": row_num(row, "Market Cap"),
            "sector": row_get(row, "Sector") or "",
            "verification": {
                "status": "mismatch" if mismatch else "ok",
                "mismatch": mismatch,
                "authoritative": "finviz",
                "price": f_price,
                "change_pct": f_change,
                "volume": f_volume,
                "avg_volume": avg_volume_shares(row),
                "rel_volume": row_num(row, "Relative Volume", "Rel Volume"),
                "short_float": row_num(row, "Short Float"),
                "earnings": row_get(row, "Earnings") or "",
            },
        }

    # -- display ------------------------------------------------------------
    @staticmethod
    def verification_line(enr: Enrichment) -> str:
        v = enr.verification or {}
        status = v.get("status")
        if status == "mismatch":
            return "DATA MISMATCH: " + "; ".join(v.get("mismatch") or []) + " (Finviz is authoritative)"
        if status == "ok":
            return "Finviz feed verified"
        if status == "token_rejected":
            return "Finviz unverified (token rejected)"
        return "Finviz unverified"

    @staticmethod
    def short_text(enr: Enrichment) -> Optional[str]:
        bits = []
        for item in enr.short_interest:
            if item.get("short_float_pct") is not None:
                bits.append(f"short float {item['short_float_pct']:.1f}% ({item.get('source')}"
                            + (f" {item['date']}" if item.get("date") else "") + ")")
            elif item.get("ratio") is not None:
                bits.append(f"SI ratio {item['ratio']:.2f} ({item.get('source')}"
                            + (f" {item['date']}" if item.get("date") else "") + ")")
            elif item.get("short_volume_ratio") is not None:
                bits.append(f"short vol {item['short_volume_ratio'] * 100:.1f}% "
                            f"({item.get('source')} {item.get('date')})")
        if enr.short_volume and enr.short_volume.get("ratio") is not None:
            bits.append(f"day short vol {enr.short_volume['ratio'] * 100:.1f}% "
                        f"({enr.short_volume.get('date')})")
        if enr.float_shares:
            bits.append(f"float {enr.float_shares:,.0f}")
        return " | ".join(bits) if bits else None
