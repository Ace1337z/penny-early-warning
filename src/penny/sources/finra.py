"""FINRA daily short-sale volume file (5.3)."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable, Optional

from .base import BaseClient, SourceError, fnum

log = logging.getLogger(__name__)

URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{ymd}.txt"


class FinraClient(BaseClient):
    name = "finra"

    def daily(self, day: Optional[date] = None, lookback: int = 7) -> dict[str, dict]:
        """Latest available CNMS short-volume file: symbol -> {short, exempt, total}."""
        start = day or date.today()
        for back in range(lookback):
            d = start - timedelta(days=back)
            if d.weekday() >= 5:
                continue
            url = URL.format(ymd=d.strftime("%Y%m%d"))
            try:
                text = self.get_text(url, timeout=25.0)
            except Exception:  # noqa: BLE001
                continue
            rows = self._parse(text)
            if rows:
                return rows
        raise SourceError("finra: no recent short-volume file found")

    @staticmethod
    def _parse(text: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        lines = text.splitlines()
        for line in lines[1:] if lines else []:
            parts = line.strip().split("|")
            if len(parts) < 5:
                continue
            d, sym, short, exempt, total = parts[0], parts[1], parts[2], parts[3], parts[4]
            sv = fnum(short)
            tv = fnum(total)
            if not sym or tv in (None, 0):
                continue
            out[sym.upper()] = {
                "date": d,
                "short_volume": sv,
                "exempt_volume": fnum(exempt),
                "total_volume": tv,
                "ratio": (sv / tv) if (sv is not None and tv) else None,
                "source": "finra",
            }
        return out

    def short_ratio(self, symbol: str) -> Optional[dict]:
        try:
            data = self.daily()
        except SourceError:
            return None
        return data.get(symbol.upper())

    def health(self) -> tuple[bool, str]:
        try:
            rows = self.daily()
            return True, f"file ok ({len(rows)} symbols)"
        except Exception as exc:  # noqa: BLE001
            return False, self.error_text(exc)
