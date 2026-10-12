"""Can this stock actually be bought, and sold, at the price on the screen?

Every other detector in the gate reads shape: a reclaim, a squeeze, a flag. This
one reads the market itself. A setup with the right shape and a 6% spread is not
a setup, it is a quote you cannot get out of, and a real trade needs the volume
to fill an order in -- not just to look impressive in a volx column.

Four hard conditions, any one of which blocks:

    C1  spread_pct > SPREAD_MAX_PCT              what you pay for being wrong
    C2  15m dollar volume < MIN_DOLLAR_VOL_15M   whether anyone is trading it
    C3  avg trade size < MIN_AVG_TRADE_SIZE      retail chop versus real size
    C4  trades in the last 15m < MIN_TRADES_15M  prints, not just volume

and two soft flags that never block, because being slightly wide is normal and
being slightly thin is a warning rather than a disqualification:

    wide_spread     1.0% <= spread_pct <= SPREAD_MAX_PCT
    retail_chop     $200 <= avg trade size <= $500

Both soft flags reach the AI, which is what they are for. A hard block reports
blocked_by="WIDE_SPREAD" when the spread is the first thing to fail and
"ILLIQUID" for everything else, because the two mean different things to the
reader: one says the market is quoting wide, the other says nobody is here.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import Bundle, StrategyResult, blocked, insufficient, passed

log = logging.getLogger(__name__)

WIDE_FLAG_PCT = 1.0
RETAIL_LOW = 200.0
RETAIL_HIGH = 500.0


def spread_pct_of(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    """(ask - bid) / mid * 100. None unless both sides are real prices."""
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if b <= 0 or a <= 0 or a < b:
        return None
    return (a - b) / ((a + b) / 2.0) * 100.0


def avg_trade_size(dollar_vol_15m: Optional[float],
                   trades_15m: Optional[float]) -> Optional[float]:
    """Average notional per print over the window."""
    try:
        d, t = float(dollar_vol_15m), float(trades_15m)
    except (TypeError, ValueError):
        return None
    if d < 0 or t <= 0:
        return None
    return d / t


def evaluate(b: Bundle, *, spread_max_pct: float = 1.5,
             min_dollar_vol_15m: float = 300000.0,
             min_avg_trade_size: float = 200.0,
             min_trades_15m: int = 30) -> StrategyResult:
    """The reality gate. `kind="blocker"`, so a BLOCK overrides every primary."""
    spread = b.spread_pct
    if spread is None:
        spread = spread_pct_of(b.bid, b.ask)

    # Finviz publishes no bid/ask, so on a Finviz-only feed this cannot be read.
    # INSUFFICIENT_DATA, not PASS: it must never count as a pass, and it must not
    # block either, or the gate would reject every symbol on a feed that simply
    # does not carry the field.
    if spread is None:
        return insufficient("spread_liquidity", "no bid/ask in the quote feed")

    dollars = b.dollars_of(15)
    trades = b.trades_15m
    avg = avg_trade_size(dollars, trades)

    evidence = {"bid": b.bid, "ask": b.ask, "spread_pct": spread,
                "dollar_vol_15m": dollars, "trades_15m": trades,
                "avg_trade_size": avg}
    failures: list[str] = []
    flags: list[str] = []

    if spread > spread_max_pct:
        failures.append("spread %.2f%% > %.2f%%" % (spread, spread_max_pct))
    elif spread >= WIDE_FLAG_PCT:
        flags.append("wide_spread")

    if dollars < float(min_dollar_vol_15m):
        failures.append("15m $%.0f < $%.0f" % (dollars, float(min_dollar_vol_15m)))

    if avg is None:
        failures.append("no trade count in the quote feed")
    elif avg < float(min_avg_trade_size):
        failures.append("avg trade $%.0f < $%.0f" % (avg, float(min_avg_trade_size)))
    elif avg <= RETAIL_HIGH:
        flags.append("retail_chop")

    if trades is not None and trades < int(min_trades_15m):
        failures.append("%d trades in 15m < %d" % (trades, int(min_trades_15m)))

    if failures:
        return blocked("spread_liquidity", failures[:4], evidence)

    reasons = ["spread %.2f%%" % spread, "15m $%.0f" % dollars]
    if avg is not None:
        reasons.append("avg trade $%.0f" % avg)
    result = passed("spread_liquidity", 0.0, reasons, evidence)
    result.flags = list(flags)
    return result
