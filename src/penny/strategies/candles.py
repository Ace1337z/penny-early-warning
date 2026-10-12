"""Bar helpers shared by the strategy detectors.

Most detectors in the spec read 1m/5m candles: a consolidation followed by a
breakout, a red-to-green cross on volume, a level that broke and was reclaimed.
None of that is visible in `Metrics`, which is a set of aggregates over the last
3 and 5 minutes.

Candles are only present for symbols that have been through
`Enricher.enrich()`. That is deliberate: the engine polls every symbol every cycle
and fetching bars for all of them would burn the TwelveData quota long before
the strategy gate could narrow the list. A detector that needs bars therefore
returns INSUFFICIENT_DATA rather than guessing at a setup that did not happen.

`bars_1m` rows are dicts with at least: "t"/"datetime" (timestamp), "o", "h",
"l", "c", "v". Older sources spell the time key differently, so everything goes
through `parse_bar`.
"""

from __future__ import annotations

from typing import Optional

# Aggregated 1m bars into this many-minute buckets.
FIVE_MIN = 5


def parse_bar(row: dict) -> Optional[dict]:
    """Normalise one source-specific bar into {ts, o, h, l, c, v}. None if unusable."""
    if not isinstance(row, dict):
        return None
    ts = row.get("t") or row.get("datetime") or row.get("timestamp") or row.get("date")
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None

    def num(*keys) -> Optional[float]:
        for k in keys:
            v = row.get(k)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    close = num("c", "close")
    if close is None or close <= 0:
        return None
    return {
        "ts": ts,
        "o": num("o", "open") or close,
        "h": num("h", "high") or close,
        "l": num("l", "low") or close,
        "c": close,
        "v": num("v", "volume") or 0.0,
    }


def clean_bars(rows) -> list[dict]:
    """Parse, drop unusable rows, sort by time."""
    out = [b for b in (parse_bar(r) for r in (rows or [])) if b]
    out.sort(key=lambda b: b["ts"])
    return out


def aggregate(bars: list[dict], minutes: int = FIVE_MIN) -> list[dict]:
    """Bucket 1m bars into N-minute OHLCV bars.

    Buckets are aligned to the epoch so two runs of the same session produce the
    same boundaries; an unaligned bucket would make a "4-candle consolidation"
    mean different things on consecutive calls.
    """
    if not bars or minutes <= 1:
        return list(bars)
    size = minutes * 60
    out: list[dict] = []
    bucket: Optional[dict] = None
    bucket_id = None
    for b in bars:
        bid = int(b["ts"] // size)
        if bucket_id != bid:
            if bucket:
                out.append(bucket)
            bucket_id = bid
            bucket = {"ts": bid * size, "o": b["o"], "h": b["h"], "l": b["l"],
                      "c": b["c"], "v": b["v"], "n": 1}
        else:
            bucket["h"] = max(bucket["h"], b["h"])
            bucket["l"] = min(bucket["l"], b["l"])
            bucket["c"] = b["c"]
            bucket["v"] += b["v"]
            bucket["n"] += 1
    if bucket:
        out.append(bucket)
    return out


def avg_volume(bars: list[dict], count: int = 5) -> float:
    """Mean volume of the last `count` bars."""
    tail = bars[-count:] if count > 0 else []
    if not tail:
        return 0.0
    return sum(b["v"] for b in tail) / float(len(tail))


def is_green(b: dict) -> bool:
    return b["c"] > b["o"]


def is_red(b: dict) -> bool:
    return b["c"] < b["o"]


def held_above(bars: list[dict], level: float, count: int) -> bool:
    """Did the last `count` bars all close at or above `level`?"""
    if count <= 0 or len(bars) < count:
        return False
    return all(b["c"] >= level for b in bars[-count:])


def minutes_below(bars: list[dict], level: float, before_last: bool = False) -> int:
    """How many bars closed below `level` in the trailing stretch.

    With `before_last`, the most recent bar is excluded: the question a VWAP
    reclaim asks is "how long was it under VWAP before the reclaim", and by the
    time price is back above it the trailing run is zero by definition.

    Counts bars, not wall-clock, so this is correct for 5m aggregates too; the
    caller multiplies by the bar size it built the series with.
    """
    seq = bars[:-1] if (before_last and bars) else bars
    n = 0
    for b in reversed(seq):
        if b["c"] < level:
            n += 1
        else:
            break
    return n


def lower_highs(bars: list[dict], count: int = 3) -> bool:
    """Three consecutive bars each making a lower high -- momentum rolling over."""
    if len(bars) < count:
        return False
    tail = bars[-count:]
    return all(tail[i]["h"] < tail[i - 1]["h"] for i in range(1, len(tail)))


def sweep_level(bars: list[dict], level: float, depth_pct: float,
                window: int = 5) -> Optional[int]:
    """Index of the most recent bar that wicked below `level` by `depth_pct`."""
    floor = level * (1.0 - depth_pct)
    for i in range(len(bars) - 1, max(0, len(bars) - 1 - window), -1):
        if bars[i]["l"] <= floor:
            return i
    return None