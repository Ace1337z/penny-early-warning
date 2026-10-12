"""Enrichment: candles, news, filings, short data, insiders, verification (6.7, 6.8)."""

from __future__ import annotations

import concurrent.futures as futures
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from .fib import FibLevels, TradePlan, fib_levels, trade_plan
from .sources.finviz import FinvizTokenRejected, avg_volume_shares, row_get, row_num
from .technicals import Technicals, analyze
from .util import ET, age_str, safe_div

log = logging.getLogger(__name__)

FILING_PREFIXES = ("8-K", "S-1", "S-3", "424B", "F-1", "F-3", "13D", "13G",
                   "SCHEDULE 13", "SC 13", "4", "6-K", "NT 10", "25")


def _parse_ts(text: str) -> Optional[datetime]:
    """Parse a feed timestamp, keeping its zone when the feed gave one.

    Order matters: the ISO forms are tried first so a `Z`/offset is not lost, then
    the naive Finviz forms, then the RSS `pubDate`.
    """
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:len(fmt) + 6], fmt)
        except ValueError:
            continue
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


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
    # Peer 15m moves as {symbol: pct}, when a peer feed provides them. No
    # source fills this yet -- sympathy_move reports INSUFFICIENT_DATA until
    # one is wired -- but the field exists so the wiring is honest, not guessed.
    sector_peers: Optional[dict] = None
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
                 sec=None, finra=None, yahoo=None, twelvedata=None, market=None):
        self.cfg = cfg
        self.finviz = finviz
        self.alpaca = alpaca
        self.finnhub = finnhub
        self.sec = sec
        self.finra = finra
        self.yahoo = yahoo
        self.twelvedata = twelvedata
        self.market = market

    # -- public -------------------------------------------------------------
    def news(self, symbol: str) -> list[dict]:
        """Merged headlines for one symbol, newest first, up to 8.

        Exists so a caller outside the enrichment path can check for news without
        re-running every candle and filing fetch behind it. The invalidation
        watcher is that caller: it needs to know whether a dilutive filing landed
        after the alert, and nothing else in the enrichment.
        """
        try:
            items, _ = self._news(symbol, fast=False)
            return items
        except Exception as exc:  # noqa: BLE001
            log.debug("news failed for %s: %s", symbol, exc)
            return []

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
            enr.bars_1m = self._live_tail(enr.bars_1m, quote)
            enr.technicals = analyze(enr.bars_1m, price)
            if enr.technicals.swing_low and enr.technicals.swing_high and price:
                enr.fib = fib_levels(enr.technicals.swing_low, enr.technicals.swing_high)
                enr.plan = trade_plan(price, enr.fib, self.cfg.float("RISK_USD", 50),
                                      min_rr=self.cfg.float("MIN_RR", 2.0))
        return enr

    @staticmethod
    def _live_tail(bars: list[dict], quote: dict) -> list[dict]:
        """Fold the live quote into the newest bar so the read is of *now*.

        Candles lag the quote by up to a whole minute and, when a source only
        has yesterday's session, by a whole day. Without this the EMA/RSI, the
        Fibonacci swing and the entry/stop were computed from a price the ticker
        had already left, which is what made the plan look like it came from the
        wrong data. The synthetic bar carries the recent median volume so it
        neither zeroes nor inflates the volume-spike test.
        """
        price = quote.get("price")
        ts = float(quote.get("ts") or 0)
        if not price or not bars or not ts:
            return bars
        last = bars[-1]
        if float(last.get("time") or 0) >= ts - 60:
            return bars  # candles already cover the quote
        vols = sorted(v for v in (float(b.get("volume") or 0) for b in bars[-60:]) if v > 0)
        volume = vols[len(vols) // 2] if vols else 0.0
        prev_close = float(last.get("close") or price)
        return list(bars) + [{"time": ts, "open": prev_close,
                              "high": max(prev_close, price),
                              "low": min(prev_close, price),
                              "close": price, "volume": volume}]

    # -- workers ------------------------------------------------------------
    def _candles(self, symbol: str) -> list[dict]:
        """1-minute bars: TwelveData -> Alpaca -> Yahoo (6.8), freshest wins.

        Finviz is the live feed and has no intraday candle endpoint. Each source
        is scored on how recent its last bar is rather than trusted blindly --
        a source that still answers happily returns its own stale history, and
        taking the first non-empty answer is what fed the plan old prices.
        `age <= 90s` is "a bar that just closed", so the best available source
        usually wins without a second request.
        """
        now = datetime.now(tz=ET)
        start = now.replace(hour=4, minute=0, second=0, microsecond=0) - timedelta(days=1)
        attempts = []
        if self.twelvedata and getattr(self.twelvedata, "configured", False):
            attempts.append(("twelvedata", lambda: self.twelvedata.bars(symbol, "1min", 390)))
        if self.alpaca and getattr(self.alpaca, "configured", False):
            attempts.append(("alpaca", lambda: self.alpaca.bars(symbol, start, now)))
        if self.yahoo:
            attempts.append(("yahoo", lambda: self.yahoo.bars(symbol, days=2)))

        best: list[dict] = []
        best_age = float("inf")
        now_epoch = time.time()
        for name, fetch in attempts:
            try:
                bars = fetch()
            except Exception as exc:  # noqa: BLE001
                log.debug("%s candles failed for %s: %s", name, symbol, exc)
                continue
            if not bars:
                continue
            age = now_epoch - float(bars[-1].get("time") or 0)
            if age <= 90:
                return bars
            if age < best_age:
                best, best_age = bars, age
        return best

    def _news(self, symbol: str, *, fast: bool = True) -> tuple[list[dict], int]:
        """Merge several sources, de-duplicate, newest first, up to 8 items (6.8).

        Finviz is asked first: it is the fastest source and the one feed already
        being polled for the quote, so a headline it carries costs no extra round
        trip. The per-symbol feeds (Alpaca, Finnhub, Google News) are slower, so
        on the alert path (`fast=True`) they are skipped once Finviz named the
        symbol. The invalidation watcher passes `fast=False`: it must still see a
        dilutive PR that only the wires carry, and missing one is the whole risk
        that watcher exists to cover.
        """
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

        if self.finviz:
            try:
                add(self._finviz_news(symbol), "finviz")
            except FinvizTokenRejected:
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("finviz news failed: %s", exc)

        if not (fast and items):
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

    def _finviz_news(self, symbol: str) -> list[dict]:
        """Per-ticker headlines from the Finviz quote page.

        The `/export/news` CSV is market-wide and matches on the headline text, so
        it misses a small cap whose PR says "SkyAI" and never "SKYA" -- which is
        exactly the alert that needs a cause. The quote page's own news table is
        keyed by ticker, so it is the source. Falls back to the market-wide CSV
        when the quote page yields nothing (older cached pages, token hiccups).
        """
        if not self.finviz:
            return []
        ticker_news = getattr(self.finviz, "ticker_news", None)
        if callable(ticker_news):
            rows = ticker_news(symbol)
            if rows:
                return rows
        sym = str(symbol or "").upper()
        out = []
        for row in (self.finviz.news("1") or [])[:60]:
            title = row_get(row, "Title", "Headline") or ""
            if sym and sym in title.upper():
                out.append({"title": title,
                            "source": row_get(row, "Source") or "finviz",
                            "url": row_get(row, "Url", "URL") or "",
                            "published": row_get(row, "Date") or ""})
        return out

    @staticmethod
    def _age_of(published) -> str:
        """Elapsed time since a feed timestamp, as "18m" / "2h" / "3d".

        The timestamp's zone is undocumented and differs per feed. Finviz's news
        export and most wires publish UTC, Alpaca carries an explicit `Z`, Google
        News carries `GMT`; only some are ET. Reading a UTC stamp as ET put a
        fresh headline hours into the future, which `age_str` then clamped to
        "0s" -- rendered as "0m ago" for every recent item. So a stamped value is
        trusted, and a naive one is read as ET and re-read as UTC when that lands
        in the future, which is the only other zone these feeds use.
        """
        if not published:
            return ""
        now = time.time()
        dt = _parse_ts(str(published).strip())
        if dt is None:
            text = str(published).strip()
            if text.isdigit():
                ts = int(text)
                if ts > 10_000_000_000:
                    ts //= 1000
                return age_str(now - ts)
            return ""
        if dt.tzinfo is not None:
            return age_str(now - dt.timestamp())
        et = dt.replace(tzinfo=ET).timestamp()
        if et > now + 60:
            et = dt.replace(tzinfo=timezone.utc).timestamp()
        return age_str(now - et)

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
