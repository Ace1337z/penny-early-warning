"""Historical analog matching: what happened to setups that looked like this one.

The forward-looking half of a prediction is a guess. The backward-looking half
is a sample. This finds the K past alerts whose features are closest to the one
being evaluated, and reports what became of them: how often they reached T1, how
often they stopped out, what the median peak was and when it came.

Every feature is z-scored within the lookback window before the Euclidean
distance is taken, because otherwise `volx` (0..40) would drown `time of day`
(0..390) and every neighbor would be "a setup at 14:00". Standardisation is
what makes the distance mean "similar" rather than "similar in whichever columns
happen to be large".

Two rules keep this honest. With fewer than ANALOG_MIN_K neighbors the block is
suppressed entirely -- a "hit rate" computed from three trades is a number that
reads like a statistic and is not one. And it never blocks: analogs inform, they
do not decide, because a sample of the past cannot outrank the live tape.
"""

from __future__ import annotations

import logging
import math
from typing import Optional, Sequence

log = logging.getLogger(__name__)

FEATURES = ("log_float", "pct_vs_close", "volx", "gap_pct", "net5d",
            "catalyst_class", "minutes_since_open", "regime")
CLASS_CODE = {"HARD": 3.0, "MEDIUM": 2.0, "SOFT": 1.0, "NONE": 0.0}


def features_of(row: dict) -> Optional[list]:
    """The feature vector for one tracked alert, or None if it is unusable."""
    try:
        flt = max(1.0, float(row.get("float_shares") or 0.0))
        return [
            math.log(flt),
            float(row.get("pct_vs_close") or 0.0),
            float(row.get("volx") or 0.0),
            float(row.get("gap_pct") or 0.0),
            float(row.get("net5d") or 0.0),
            CLASS_CODE.get(str(row.get("catalyst_class") or "NONE").upper(), 0.0),
            float(row.get("minutes_since_open") or 0.0),
            float(row.get("regime") or 0.0),
        ]
    except (TypeError, ValueError):
        return None


def _zscore(values: Sequence) -> list:
    """Standardise one column. A constant column scores flat rather than noise."""
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 2:
        return [0.0] * n
    mean = sum(vals) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / n)
    if sd < 1e-9:
        return [0.0] * n
    return [(v - mean) / sd for v in vals]


def zscore_columns(vectors: Sequence[Sequence]) -> list:
    """Standardise each feature column. Returns columns, not rows."""
    if not vectors:
        return []
    width = len(vectors[0])
    cols = []
    for i in range(width):
        cols.append(_zscore([v[i] for v in vectors]))
    return cols


def _distance(a: Sequence, b: Sequence) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _median(values) -> Optional[float]:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def find_neighbors(target: Sequence, pool: Sequence[dict], *, k: int = 20,
                   min_k: int = 8) -> tuple:
    """The k nearest past alerts to `target`, as (neighbor rows, distances).

    Rows without a usable feature vector are skipped rather than zero-filled: a
    zero vector is a fake point at the mean of the set and would come back as a
    "close match" to everything.
    """
    usable = [(r, features_of(r)) for r in (pool or [])]
    usable = [(r, f) for r, f in usable if f]
    if not usable or not target:
        return [], []

    # Standardise the pool and the target together so both are in the same units.
    vectors = [f for _, f in usable] + [list(target)]
    cols = zscore_columns(vectors)
    target_z = [cols[i][-1] for i in range(len(cols))]
    scored = []
    for j, (row, _) in enumerate(usable):
        point = [cols[i][j] for i in range(len(cols))]
        scored.append((_distance(target_z, point), row))
    scored.sort(key=lambda pair: pair[0])
    top = scored[:max(1, int(k))]
    return [row for _, row in top], [d for d, _ in top]


def summarize(neighbors: Sequence[dict], *, min_k: int = 8) -> Optional[dict]:
    """T1 rate, stop rate, median peak and median minutes for the neighbors.

    Returns None below `min_k`: an outcome summary from too few trades is
    suppressed rather than printed, so the reader is never shown a percentage
    that rests on three observations.
    """
    rows = list(neighbors or [])
    n = len(rows)
    if n < max(2, int(min_k)):
        return None
    out = {
        "n": n,
        "t1_hit_rate": sum(1 for r in rows if r.get("hit_t1")) / n,
        "stop_hit_rate": sum(1 for r in rows if r.get("hit_stop")) / n,
        "median_peak_pct": _median([r.get("max_gain_pct") for r in rows]),
        "median_peak_minutes": _median([r.get("peak_minutes") for r in rows]),
    }
    return out


def analog_line(summary: Optional[dict]) -> str:
    """The ANALOGS line for the AI DECISION block, or "" when suppressed."""
    if not summary:
        return ""
    head = "ANALOGS: %d similar setups -- T1 %d%%, stop %d%%" % (
        summary["n"], round(summary["t1_hit_rate"] * 100),
        round(summary["stop_hit_rate"] * 100))
    peak = summary.get("median_peak_pct")
    if peak is None:
        return head
    tail = "median peak %+.1f%%" % (float(peak) * 100.0)
    if summary.get("median_peak_minutes") is not None:
        tail += " @ %dm" % round(float(summary["median_peak_minutes"]))
    return "%s, %s" % (head, tail)
