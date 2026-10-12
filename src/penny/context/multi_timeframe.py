"""Multi-timeframe context: the shape behind today's move, for the reader only.

An intraday alert arrives with one frame in view: the last three and five
minutes. That frame is enough to time an entry and not enough to judge one, and
the numbers here answer the questions a screener leaves open:

    daily trend           is this stock already extended above its own 20-day mean
    daily range position  is today's bar near the top of the last month's range
    weekly float turnover has the float already been turned over this week
    prior 5d shape        has this name been green or red before today

The block is AI-only. It is appended to the AI user message and never rendered
into Alert 1 or Alert 2: the alert stays lean, and a five-frame table would undo
that. The AI may still refer to it in `key_findings`.

Every field is optional. A missing daily history yields "unknown" rather than a
guess, because a fabricated 20-day moving average is worse than an honest blank
-- and because the system is long-only, where "already extended" is the single
most expensive error to invent.
"""

from __future__ import annotations

from typing import Optional, Sequence

MA_DAYS = 20
RANGE_DAYS = 20
SHAPE_DAYS = 5


def _floats(values: Sequence, limit: Optional[int] = None) -> list:
    """The usable positive numbers in a sequence, oldest first."""
    out = []
    for v in (values or []):
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out.append(f)
    return out[-limit:] if limit else out


def daily_ma(closes: Sequence, days: int = MA_DAYS) -> Optional[float]:
    """Simple moving average over the last `days` closes.

    Needs at least half the window before it will answer: a 3-day mean called a
    "20-day MA" is a different number wearing the same label.
    """
    values = _floats(closes, days)
    if len(values) < max(2, days // 2):
        return None
    return sum(values) / len(values)


def range_position(day_high: Optional[float], lows: Sequence,
                   days: int = RANGE_DAYS) -> Optional[float]:
    """Where today's high sits inside the last `days` of lows, 0.0..1.0.

    1.0 means today's high is above every low in the window: this is the top of
    the range. Comparing today's high against the window's lows answers "is today
    the highest point of the month" without needing the window's highs.
    """
    high = _floats([day_high])
    if not high:
        return None
    values = _floats(lows, days)
    if len(values) < 3:
        return None
    lo = min(values)
    span = max(values) - lo
    if span <= 0:
        return None
    return max(0.0, min(1.0, (high[0] - lo) / span))


def weekly_float_turnover(week_volume: Optional[float],
                          float_shares: Optional[float]) -> Optional[float]:
    """Volume traded this week as a multiple of the float."""
    vol, flt = _floats([week_volume]), _floats([float_shares])
    if not vol or not flt:
        return None
    return vol[0] / flt[0]


def prior_shape(closes: Sequence, prev_close: Optional[float],
                days: int = SHAPE_DAYS) -> list:
    """Closing % for each of the last `days` sessions, oldest first.

    `prev_close` is today's prior close, so the most recent entry is today's own
    move -- which is the point: the shape that matters is "flat before today",
    not a shape that already includes the move being judged.
    """
    values = _floats(closes)
    today = _floats([prev_close])
    series = values[-days - 1:] if not today else values[-days:] + today
    out = []
    for i in range(1, len(series)):
        if series[i - 1] > 0:
            out.append(series[i] / series[i - 1] - 1.0)
    return out[-days:]


def build_context(daily: Optional[Sequence] = None,
                  week_volume: Optional[float] = None,
                  float_shares: Optional[float] = None,
                  day_high: Optional[float] = None,
                  prev_close: Optional[float] = None,
                  rise_60: Optional[float] = None) -> dict:
    """Assemble the AI-only context block.

    `daily` is the symbol's completed sessions, oldest first, each with `close`
    and `low`. It is the same `daily_close` history the detectors read, so this
    costs no extra fetch -- and on a fresh install it is empty, in which case
    the block degrades to the intraday fields it does have.
    """
    rows = [r for r in (daily or []) if isinstance(r, dict)]
    ma = daily_ma([r.get("close") for r in rows])
    pos = range_position(day_high, [r.get("low") for r in rows])
    turn = weekly_float_turnover(week_volume, float_shares)
    shape = prior_shape([r.get("close") for r in rows], prev_close)

    if ma and prev_close and float(prev_close) > 0:
        diff = (float(prev_close) / ma - 1.0) * 100.0
        if diff >= 0:
            trend = "above 20d MA (%.2f, %+.1f%%)" % (ma, diff)
        else:
            # Below the mean is the case that matters most for a long-only
            # system: "already extended" is the expensive error, and calling a
            # stock under its own mean "above" with a negative percentage is a
            # label that contradicts its own number.
            trend = "below 20d MA (%.2f, %+.1f%%)" % (ma, diff)
    elif ma:
        trend = "below 20d MA (%.2f)" % ma
    else:
        trend = "unknown (no daily history)"
    r60 = _floats([rise_60])
    return {
        "daily_trend": trend,
        "daily_range_position": ("top %d%% of 20-day base" % round((1.0 - pos) * 100.0))
        if pos is not None else "unknown",
        "weekly_float_turnover": ("%.1fx" % turn) if turn is not None else "unknown",
        "prior_5d_shape": ("[%s] (%s)" % (", ".join("%+.1f" % (c * 100) for c in shape),
                                          "net green" if sum(shape) > 0 else "net red"))
        if shape else "unknown",
        "trend_60m": ("%+.1f%%" % (r60[0] * 100.0)) if r60 else "unknown",
    }


def block_lines(ctx: Optional[dict]) -> str:
    """The MULTI-TIMEFRAME CONTEXT section for the AI user message.

    Returns "" when MTF is off or nothing was computed, so the prompt never
    carries a heading with nothing under it.
    """
    if not ctx:
        return ""
    return "\n".join([
        "MULTI-TIMEFRAME CONTEXT (for reasoning only; not shown to the user)",
        "  daily trend: %s" % ctx.get("daily_trend", "unknown"),
        "  daily range position: %s" % ctx.get("daily_range_position", "unknown"),
        "  weekly float turnover: %s" % ctx.get("weekly_float_turnover", "unknown"),
        "  prior 5d shape: %s" % ctx.get("prior_5d_shape", "unknown"),
        "  60m trend: %s" % ctx.get("trend_60m", "unknown"),
    ])
