"""Finnhub news (5.3)."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable, Optional

from .base import BaseClient, SourceError

log = logging.getLogger(__name__)

BASE = "https://finnhub.io/api/v1"


class FinnhubClient(BaseClient):
    name = "finnhub"

    def __init__(self, cfg, secrets: Optional[Iterable[str]] = None):
        super().__init__(cfg, secrets)
        self.key = cfg.raw("FINNHUB_KEY")

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def company_news(self, symbol: str, days: int = 3) -> list[dict]:
        if not self.configured:
            raise SourceError("finnhub: not configured")
        today = date.today()
        params = {
            "symbol": symbol.upper(),
            "from": (today - timedelta(days=days)).isoformat(),
            "to": today.isoformat(),
            "token": self.key,
        }
        data = self.get_json(f"{BASE}/company-news", params=params)
        out = []
        for item in (data if isinstance(data, list) else []):
            out.append({
                "title": item.get("headline") or "",
                "source": item.get("source") or "finnhub",
                "url": item.get("url") or "",
                "published": str(item.get("datetime") or ""),
                "summary": item.get("summary") or "",
            })
        return out

    def health(self) -> tuple[bool, str]:
        if not self.configured:
            return False, "not configured (optional)"
        try:
            news = self.company_news("AAPL", days=2)
            return True, f"news ok ({len(news)} items)"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
