"""Small, pure numeric helpers the strategy and risk layers share.

Nothing here reads the network, the clock, or configuration. Every function is
a deterministic transform of its arguments, which is what lets the phase
classifier, the confirmation machine and the level engine all be tested with
plain numbers.

Three groups:

  - Bar math: EMA over closes, the range-position of a close inside its own bar.
    Used by the supernova classifier (Spec 2.3 wants an EMA9 extension check).
  - Volume profile: cumulative expected volume at minute t, for time-of-day
    relative volume (Spec 2.2 -- compare against the minute profile, not the
    daily average).
  - Structural levels: overhead resistance (prior highs, whole/half-dollar
    marks, consolidation pivots) for the risk layer (Spec 4.2).
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence


# --------------------------------------------------------------------------
# bar math
# --------------------------------------------------------------------------

def ema(values: Sequence[float], period: int) -> Optional[float]:
    """Exponential moving average of the tail of ``values``.

    Returns None when there is not yet one full period of data, rather than a
    seed value that would be wrong on the first bars -- a phase decided off a
    half-formed EMA is worse than no phase at all.
    """
    if period <= 0 or values is None:
        return None
    vals = [float(v) for v in values if v is not None]
    if len(vals) < period:
        return None
    k = 2.0 / (period + 1.0)
    out = sum(vals[:period]) / period
    for v in vals[period:]:
        out = v * k + out * (1.0 - k)
    return out


def closes(bars: Iterable[dict]) -> list[float]:
    """The close of every bar that has one, in order."""
    out = []
    for b in bars or []:
        c = b.get("c")
        if c is not None:
            out.append(float(c))
    return out


def range_position(bar: dict) -> Optional[float]:
    """Where the close sits inside the bar, 0 = low, 1 = high.

    Spec 3.1 Primitive A asks for a close in the upper 50% of the bar's range;
    a zero-range bar (open == high == low == close) has no position to speak of
    and returns None so the caller does not read a fabricated 0.5.
    """
    if not bar:
        return None
    try:
        h = float(bar["h"])
        low = float(bar["l"])
        c = float(bar["c"])
    except (KeyError, TypeError, ValueError):
        return None
    if h <= low:
        return None
    return (c - low) / (h - low)


def bar_low(bars: Sequence[dict], idx: int = -1) -> Optional[float]:
    """Low of the bar at ``idx`` (defaults to the last closed bar)."""
    try:
        return float(bars[idx]["l"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# time-of-day volume profile (Spec 2.2)
# --------------------------------------------------------------------------

# Fraction of the day's volume expected to have traded by the end of each of the
# first ten minutes of the regular session. A US small-cap's volume front-loads
# hard: the open minute alone is a large slice of the day. These are population
# priors, deliberately simple, and they are only ever used to *scale* the
# measured pace -- the engine still compares against the stock's own average.
OPEN_PROFILE = (0.06, 0.105, 0.140, 0.170, 0.196, 0.219, 0.240, 0.259, 0.277, 0.294)


def expected_cum_fraction(minutes_into_session: float) -> float:
    """Expected share of a full day's volume traded after ``minutes`` of the open.

    Linear after the first ten minutes up to the close (390 minutes is a full
    regular session), so the curve is continuous and monotonic. Returns a value
    in (0, 1].
    """
    m = max(0.0, float(minutes_into_session or 0.0))
    if m <= 0:
        return OPEN_PROFILE[0]
    if m < len(OPEN_PROFILE):
        lo = OPEN_PROFILE[int(m) - 1]
        hi = OPEN_PROFILE[int(m)]
        return lo + (hi - lo) * (m - int(m))
    tail = OPEN_PROFILE[-1]
    rest = (m - (len(OPEN_PROFILE) - 1)) / (390.0 - (len(OPEN_PROFILE) - 1))
    return min(1.0, tail + (1.0 - tail) * max(0.0, rest))


def tod_rvol(cum_volume: float, avg_daily_volume: Optional[float],
             minutes_into_session: float) -> Optional[float]:
    """Time-of-day relative volume.

    ``cum_volume`` so far vs the volume a normal day would have traded by this
    minute: ``cum / (avg_daily * expected_fraction(minutes))``. This is the
    honest "is anyone actually here" number; a raw cum/average would call every
    symbol quiet at 09:35 and busy at 15:55. None when the average is unknown --
    never a guess.
    """
    if not avg_daily_volume or avg_daily_volume <= 0:
        return None
    expected = avg_daily_volume * expected_cum_fraction(minutes_into_session)
    if expected <= 0:
        return None
    return max(0.0, float(cum_volume or 0.0) / expected)


# --------------------------------------------------------------------------
# float turnover (Spec 2.2)
# --------------------------------------------------------------------------

def float_turnover(cum_volume: float, float_shares: Optional[float]) -> Optional[float]:
    """Cumulative intraday volume / verified float. None when float is unknown."""
    if not float_shares or float_shares <= 0:
        return None
    return max(0.0, float(cum_volume or 0.0) / float_shares)


# --------------------------------------------------------------------------
# structural levels (Spec 4.2)
# --------------------------------------------------------------------------

def whole_half_marks(price: float, above: float, below: float,
                     step: float = 0.5) -> list[float]:
    """Whole- and half-dollar marks within ``[price-below, price+above]``.

    Round numbers are where a lot of small-cap supply sits, so they belong on an
    overhead list even when no candle defines them.
    """
    if price <= 0:
        return []
    out = []
    lo = price - max(0.0, below)
    hi = price + max(0.0, above)
    mark = int(lo / step) * step
    while mark <= hi + 1e-9:
        if lo <= mark <= hi:
            out.append(round(mark, 4))
        mark += step
    return out


def overhead_levels(price: float, day_high: Optional[float],
                    prior_highs: Optional[Iterable[float]] = None,
                    pivots: Optional[Iterable[float]] = None,
                    extra: Optional[Iterable[float]] = None,
                    max_marks: float = 2.0) -> list[float]:
    """Real resistance above the current price, deduped and sorted ascending.

    Spec 4.2 replaces fixed T1/T2 percentages with structural overhead:
    the session high, prior-day/consolidation highs, and whole/half-dollar marks.
    Only levels *above* price are returned -- "resistance below you" is a
    contradiction. The list is ascending and capped to the nearest few, because
    a third level past the second is trivia rather than a plan.
    """
    price = float(price or 0.0)
    if price <= 0:
        return []
    raw: list[float] = []
    if day_high and day_high > price:
        raw.append(float(day_high))
    for seq in (prior_highs, pivots, extra):
        for v in seq or []:
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v > price:
                raw.append(v)
    raw.extend(m for m in whole_half_marks(price, above=max_marks, below=0.0) if m > price)

    out: list[float] = []
    for v in sorted(raw):
        if not out or abs(v - out[-1]) / price > 0.002:
            out.append(round(v, 4))
    return out[:3]
