"""Calibrate the AI's confidence against what the tracker actually recorded.

An AI that says 0.80 on every setup it likes is not calibrated; it is
enthusiastic. The tracker is the only honest source of truth here, so once a
given (rating, primary_strategy) bucket has accumulated enough closed alerts,
its historical T1 hit rate becomes the ceiling on what confidence the reader is
shown. The AI's own number is kept -- it may know something the sample does
not -- but it is not allowed to sit far above the bucket's realised hit rate.

The margin is deliberately a cushion, not equality: a bucket that has hit T1
64% of the time still allows 0.79 confidence (64% + 15%). Forcing exact
equality would punish a genuinely strong setup in a merely average bucket, and
the whole point of the rating is to catch that case.

Buckets below CALIBRATION_MIN_SAMPLES are "insufficient": no prior, no clamp,
the raw confidence is used unchanged. Inventing a prior from six alerts is the
exact failure this module exists to prevent.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from ..util import now_et, safe_div

log = logging.getLogger(__name__)

UNRATED = "unrated"


def bucket_key(rating: Optional[str], primary: Optional[str],
               cause_present: Optional[bool] = None) -> str:
    """The (rating, strategy, cause) key (Spec 2.0 D1).

    Split by cause because a MODERATE rating on a named catalyst and the same
    rating on a cause-free tape move are different bets, not one bucket.
    Unknown values collapse to one bucket each.
    """
    return "%s|%s|%s" % (str(rating or UNRATED).upper(), str(primary or UNRATED),
                         "cause" if cause_present else "no_cause")


def build_table(rows: Sequence[dict], *, window_days: int = 90,
                min_samples: int = 30) -> dict:
    """Aggregate closed alerts into per-bucket priors over the window.

    Rows are closed alerts -- an open or aborted one has no outcome yet, and
    counting it would flatter a bucket with alerts the tracker has not finished
    measuring.
    """
    cutoff = (now_et() - __import__("datetime").timedelta(days=int(window_days))
              ).strftime("%Y-%m-%d")
    groups: dict[str, list] = {}
    for r in rows or []:
        if (r.get("day") or "") < cutoff:
            continue
        if r.get("status") != "closed":
            continue
        groups.setdefault(bucket_key(r.get("ai_rating"), r.get("primary_strategy"),
                                     bool(r.get("cause_present"))),
                          []).append(r)

    table: dict = {}
    for key, group in groups.items():
        n = len(group)
        entry = {"alerts": n, "sufficient": n >= int(min_samples)}
        if entry["sufficient"]:
            entry["t1_hit_rate"] = safe_div(sum(1 for r in group if r.get("hit_t1")), n)
            entry["stop_hit_rate"] = safe_div(sum(1 for r in group if r.get("hit_stop")), n)
            entry["median_close_pct"] = _median([r.get("close_pct") for r in group])
            entry["median_max_gain_pct"] = _median([r.get("max_gain_pct") for r in group])
        table[key] = entry
    return table


def _median(values) -> Optional[float]:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def prior_for(table: dict, rating: Optional[str], primary: Optional[str],
              cause_present: Optional[bool] = None) -> Optional[dict]:
    """The sufficient-sample prior for a bucket, or None."""
    entry = (table or {}).get(bucket_key(rating, primary, cause_present))
    if not entry or not entry.get("sufficient"):
        return None
    return entry


def clamp_confidence(confidence: float, prior: Optional[dict], margin: float = 0.15):
    """Clamp AI confidence to the bucket's hit rate plus the margin.

    Returns (calibrated_confidence, flag). The flag is "" when no clamp applied,
    and "confidence_clamped_to_history" when it did -- which is recorded so the
    reader of /stats can tell an honest 0.4 from a clamped one.
    """
    if not prior or prior.get("t1_hit_rate") is None:
        return confidence, ""
    ceiling = float(prior["t1_hit_rate"]) + float(margin)
    if confidence <= ceiling:
        return confidence, ""
    return ceiling, "confidence_clamped_to_history"


def prior_lines(prior: Optional[dict], label: str = "") -> str:
    """The CALIBRATION PRIOR block for the AI user message, or ""."""
    if not prior:
        return ""
    head = "CALIBRATION PRIOR%s" % (" (%s)" % label if label else "")
    bits = ["alerts: %d" % prior.get("alerts", 0)]
    if prior.get("t1_hit_rate") is not None:
        bits.append("T1 hit: %d%%" % round(prior["t1_hit_rate"] * 100))
    if prior.get("stop_hit_rate") is not None:
        bits.append("stop: %d%%" % round(prior["stop_hit_rate"] * 100))
    if prior.get("median_close_pct") is not None:
        bits.append("median close: %+.1f%%" % (prior["median_close_pct"] * 100.0))
    return "\n".join([head, "  " + "  ".join(bits)])
