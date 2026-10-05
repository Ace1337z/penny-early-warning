"""Alpaca: 1-minute candles (fallback) and news (5.3)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .base import BaseClient, SourceError, fnum

log = logging.getLogger(__name__)

DATA_BASE = "https://data.alpaca.markets"


class AlpacaClient(BaseClient):
    name = "alpaca"

    def __init__(self, cfg, secrets: Optional[Iterable[str]] = None):
        super().__init__(cfg, secrets)
        self.key = cfg.raw("ALPACA_KEY")
        self.secret = cfg.raw("ALPACA_SECRET")
        self.feed = cfg.raw("ALPACA_FEED") or "iex"

    @property
    def configured(self) -> bool:
        return bool(self.key and self.secret)

    def _headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

    def bars(self, symbol: str, start: datetime, end: datetime,
             timeframe: str = "1Min", limit: int = 10000) -> list[dict]:
        if not self.configured:
            raise SourceError("alpaca: not configured")
        url = f"{DATA_BASE}/v2/stocks/{symbol.upper()}/bars"
        params = {
            "timeframe": timeframe,
            "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": limit,
            "adjustment": "raw",
            "feed": self.feed,
        }
        data = self.get_json(url, headers=self._headers(), params=params)
        out = []
        for b in (data.get("bars") or []):
            try:
                t = b.get("t")
                if isinstance(t, str):
                    t = datetime.fromisoformat(t.replace("Z", "+00:00"))
                ts = int(t.timestamp()) if isinstance(t, datetime) else int(t)
                out.append({"time": ts, "open": fnum(b.get("o")), "high": fnum(b.get("h")),
                            "low": fnum(b.get("l")), "close": fnum(b.get("c")),
                            "volume": fnum(b.get("v")) or 0.0})
            except (TypeError, ValueError, AttributeError):
                continue
        out.sort(key=lambda x: x["time"])
        return out

    def news(self, symbols: Iterable[str], hours: int = 72, limit: int = 50) -> list[dict]:
        if not self.configured:
            raise SourceError("alpaca: not configured")
        url = f"{DATA_BASE}/v1beta1/news"
        params = {
            "symbols": ",".join(s.upper() for s in symbols),
            "limit": limit,
            "start": (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
        }
        data = self.get_json(url, headers=self._headers(), params=params)
        out = []
        for item in (data.get("news") or []):
            out.append({
                "title": item.get("headline") or "",
                "source": item.get("source") or "alpaca",
                "url": item.get("url") or "",
                "published": item.get("created_at") or "",
                "summary": item.get("summary") or "",
            })
        return out

    def health(self) -> tuple[bool, str]:
        if not self.configured:
            return False, "not configured (optional)"
        try:
            end = datetime.now(timezone.utc)
            bars = self.bars("AAPL", end - timedelta(days=3), end)
            return True, f"bars ok ({len(bars)} bars)"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
