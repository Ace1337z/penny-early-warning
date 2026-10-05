"""Yahoo / Google News: candle fallback, index levels, news RSS (5.3).

Uses yfinance when it is installed, otherwise the public chart endpoint directly.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Iterable, Optional

from .base import BaseClient, SourceError, fnum

log = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}"


class YahooClient(BaseClient):
    name = "yahoo"

    # -- candles ------------------------------------------------------------
    def bars(self, symbol: str, days: int = 1, interval: str = "1m") -> list[dict]:
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            return self._bars_http(symbol, days=days, interval=interval)
        try:
            period = f"{max(1, days)}d" if days <= 7 else "1mo"
            df = yf.Ticker(symbol).history(period=period, interval=interval,
                                           prepost=True, auto_adjust=False)
            out = []
            for idx, row in df.iterrows():
                ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(idx)
                out.append({"time": ts, "open": float(row["Open"]), "high": float(row["High"]),
                            "low": float(row["Low"]), "close": float(row["Close"]),
                            "volume": float(row.get("Volume") or 0)})
            return out
        except Exception as exc:  # noqa: BLE001
            log.debug("yfinance failed for %s (%s); trying HTTP", symbol, exc)
            return self._bars_http(symbol, days=days, interval=interval)

    def _bars_http(self, symbol: str, days: int = 1, interval: str = "1m") -> list[dict]:
        rng = "1d" if days <= 1 else ("5d" if days <= 5 else "1mo")
        url = CHART_URL.format(sym=symbol)
        data = self.get_json(url, params={"interval": interval, "range": rng,
                                          "includePrePost": "true"})
        try:
            result = data["chart"]["result"][0]
            times = result["timestamp"]
            quote = result["indicators"]["quote"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise SourceError(f"yahoo: no chart data for {symbol}") from exc
        out = []
        for i, ts in enumerate(times):
            try:
                out.append({
                    "time": int(ts),
                    "open": quote["open"][i],
                    "high": quote["high"][i],
                    "low": quote["low"][i],
                    "close": quote["close"][i],
                    "volume": quote["volume"][i] or 0,
                })
            except (IndexError, KeyError, TypeError):
                continue
        return [b for b in out if b["close"]]

    # -- index levels -------------------------------------------------------
    def index_levels(self, symbols: Iterable[str] = ("^GSPC", "^IXIC", "^VIX")) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for sym in symbols:
            try:
                data = self.get_json(CHART_URL.format(sym=sym),
                                     params={"interval": "1d", "range": "5d"})
                result = data["chart"]["result"][0]
                meta = result.get("meta") or {}
                price = meta.get("regularMarketPrice")
                prev = meta.get("chartPreviousClose") or meta.get("previousClose")
                if price and prev:
                    out[sym] = {"price": float(price), "prev_close": float(prev),
                                "change_pct": (float(price) / float(prev) - 1) * 100}
            except Exception as exc:  # noqa: BLE001
                log.debug("yahoo index %s failed: %s", sym, exc)
        return out

    # -- news ---------------------------------------------------------------
    def google_news(self, symbol: str, limit: int = 8) -> list[dict]:
        q = f"{symbol}+stock"
        try:
            text = self.get_text(GOOGLE_NEWS.format(q=q), timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"yahoo/google news failed: {exc}") from exc
        return self._parse_rss(text, limit)

    @staticmethod
    def _parse_rss(text: str, limit: int) -> list[dict]:
        out = []
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return out
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if not title:
                continue
            out.append({"title": title, "url": link, "published": pub,
                        "source": "google-news", "summary": ""})
            if len(out) >= limit:
                break
        return out

    def health(self) -> tuple[bool, str]:
        try:
            levels = self.index_levels(("^GSPC",))
            if levels:
                return True, f"ok (^GSPC {levels['^GSPC']['price']:.0f})"
            return False, "no index data returned"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
