"""Sector rotation: is this stock moving with its sector or against it?

A $0.40 low-float name up 30% on nothing in particular is a very different
setup from the same name up 30% while eight of its sector peers are also up,
which is flow arriving rather than one retail order. The heatmap answers that
from the movers list the engine already holds, so it costs no fetch: group
today's ranked names by sector, take the median move in each, and call a sector
hot when enough of its names are on the list or the median is large enough.

The median, not the mean: one +900% name in a sector of otherwise flat ones
would make the mean lie about every other stock in it.

The SECTOR line is shown only when the candidate's sector is hot. A cold-sector
line tells the reader nothing they did not already know from the price.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

log = logging.getLogger(__name__)


class SectorHeatmap:
    """Rolling 1h heatmap built from the movers list. Refresh is rate-limited."""

    def __init__(self, min_hot_pct: float = 2.0, top_n: int = 3,
                 refresh_seconds: int = 300, clock=None):
        self.min_hot_pct = float(min_hot_pct)
        self.top_n = int(top_n)
        self.refresh_seconds = max(1, int(refresh_seconds))
        self.clock = clock or time.time
        self._rows: list[dict] = []
        self._ts = 0.0

    # -- build --------------------------------------------------------------
    def update(self, rows: Optional[Iterable[dict]]) -> None:
        """Take a new movers list if the refresh interval has elapsed."""
        now = self.clock()
        if self._rows and now - self._ts < self.refresh_seconds:
            return
        self._rows = [r for r in (rows or []) if isinstance(r, dict)]
        self._ts = now

    @property
    def rows(self) -> list[dict]:
        return self._rows

    # -- read ---------------------------------------------------------------
    def sector_stats(self) -> dict:
        """sector -> count / median_pct / hot."""
        buckets: dict[str, list] = {}
        for row in self._rows:
            sector = str(row.get("sector") or "").strip().lower()
            if not sector or row.get("change_pct") is None:
                continue
            try:
                buckets.setdefault(sector, []).append(float(row["change_pct"]))
            except (TypeError, ValueError):
                continue
        out: dict = {}
        for sector, pcts in buckets.items():
            pcts.sort()
            mid = len(pcts) // 2
            median = pcts[mid] if len(pcts) % 2 else (pcts[mid - 1] + pcts[mid]) / 2.0
            count = len(pcts)
            out[sector] = {"count": count, "median_pct": median,
                           "hot": median >= self.min_hot_pct or count >= self.top_n}
        return out

    def for_symbol(self, sector: str) -> Optional[dict]:
        if not sector:
            return None
        return self.sector_stats().get(str(sector).strip().lower())

    def float_rank(self, sector: str, float_shares: Optional[float],
                   sector_floats: Optional[dict]) -> Optional[int]:
        """1-based rank of this float inside its sector, thinnest first.

        `sector_floats` maps symbol -> float for the sector's peers, which the
        caller reads from the same movers list. Returns None rather than a rank
        of 1 when there is nothing to compare against -- "thinnest of one" is not
        a fact.
        """
        if not sector or not float_shares or not sector_floats:
            return None
        peers = sorted(float(v) for v in sector_floats.values() if float(v or 0) > 0)
        if not peers:
            return None
        peers.append(float(float_shares))
        peers.sort()
        return peers.index(float(float_shares)) + 1


def sector_line(heatmap: SectorHeatmap, sector: str, float_shares: Optional[float] = None,
                sector_floats: Optional[dict] = None) -> str:
    """The SECTOR line for the AI DECISION block, or "" when the sector is cold.

    Cold is the default case and produces no line at all: the spec is explicit
    that this appears only for a hot sector, and a line reading "not a hot
    sector" would be padding the lean message.
    """
    stats = heatmap.for_symbol(sector)
    if not stats or not stats.get("hot"):
        return ""
    bits = ["SECTOR: %s %+.1f%% (hot, %d names on the list)"
            % (str(sector).lower(), stats["median_pct"], stats["count"])]
    rank = heatmap.float_rank(sector, float_shares, sector_floats)
    if rank and rank > 1:
        bits.append("| %d thinnest float in the sector" % rank)
    return " ".join(bits)
