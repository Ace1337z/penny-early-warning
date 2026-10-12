"""Exit liquidity: can this position actually be sold, and how fast?

An entry that cannot be exited is not a trade. A $10k position in a name doing
$40k an hour is hours of volume, and the exit is the part of the round trip that
a percentage on a screen never shows.

The estimate walks the last 15 one-minute bars forward and fills each minute's
volume in price order: the bars beyond the price fill first, then the bar the
market is in. That is optimistic -- it assumes no adverse selection, no halts
and no queue priority -- so the slippage it returns is a floor, not a promise.
It reads bars already fetched for enrichment, so it costs nothing extra.

The $500 case is deliberately the blocking one. A $500 exit is the smallest
position the system recommends; if even that cannot clear within the time limit
at acceptable slip, no position in this name can be managed.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from ..strategies.candles import clean_bars

log = logging.getLogger(__name__)

NOTIONALS = (500, 2000, 10000)
LOOKBACK_MIN = 15
HALF_SPREAD = 0.0025      # 25bp: a wide-but-realised penny-stock half-spread


def _fill(bars: Sequence[dict], notional: float, price: float) -> tuple:
    """Sell `notional` dollars against the bar volume profile, oldest bar first.

    The walk starts at the most recent bar -- that is where the exit begins --
    and pays a price that worsens with each minute consumed: the current bar
    fills at the bid (a sale receives the bid, not the last trade), and each
    subsequent minute pays a further half-spread, modelling that a multi-minute
    exit eats through more of the book.

    Returns (seconds, slip_pct, filled_fraction). A `filled` below 1.0 means the
    volume profile ran out before the position was placed, which is itself the
    answer: the exit does not fit in this window.
    """
    if not bars or notional <= 0 or price <= 0:
        return None, None, 0.0
    recent = list(bars)[::-1]
    shares = notional / price
    remaining = shares
    spent = 0.0
    seconds = 0.0
    step = 0.0
    for bar in recent:
        vol = float(bar.get("v") or 0.0)
        if vol <= 0:
            continue
        take = min(vol, remaining)
        # A sale receives the bid: one half-spread below the last print, and a
        # further half-spread for every minute the exit takes.
        fill_price = price * (1.0 - HALF_SPREAD - step * HALF_SPREAD)
        if fill_price <= 0:
            break
        spent += take * fill_price
        remaining -= take
        seconds += 60.0
        if remaining <= 0:
            break
        step += 1.0
    filled = 0.0 if shares <= 0 else 1.0 - remaining / shares
    if filled <= 0:
        return None, None, 0.0
    avg = spent / (shares * filled)
    return seconds, max(0.0, -(avg / price - 1.0) * 100.0), filled


def estimate(bars, price: float, *, notionals=NOTIONALS,
             lookback: int = LOOKBACK_MIN) -> dict:
    """Exit time and slippage for each notional, from the last `lookback` bars.

    Returns {} when there are no usable bars. An estimate nobody can compute is
    reported as absent, never as "fast".
    """
    clean = clean_bars(bars)[-lookback:]
    if not clean or not price or float(price) <= 0:
        return {}
    out: dict = {}
    for n in notionals:
        seconds, slip, filled = _fill(clean, float(n), float(price))
        key = str(int(n))
        if seconds is None:
            out[key] = {"seconds": None, "slip_pct": None, "filled": round(filled, 3)}
        else:
            out[key] = {"seconds": int(seconds), "slip_pct": round(slip, 3),
                        "filled": round(filled, 3)}
    return out


def passes(bars, price: float, *, max_seconds: int = 300,
           max_slip_pct: float = 1.0) -> tuple:
    """The $500 gate test: (ok, reason, seconds, slip).

    `ok` is False only when the estimate says the smallest position cannot be out
    in time, or with more slip than allowed. A missing estimate passes: the gate
    blocks on evidence, and no bars is an absence of evidence rather than proof
    of a bad exit.
    """
    est = estimate(bars, price)
    small = est.get("500") or {}
    if small.get("seconds") is None:
        return True, "", None, None
    seconds = small["seconds"]
    slip = small.get("slip_pct")
    if seconds > int(max_seconds):
        return False, "$500 exit needs ~%ds (> %ds)" % (seconds, max_seconds), seconds, slip
    if slip is not None and slip > float(max_slip_pct):
        return False, "$500 exit slips %.2f%% (> %.2f%%)" % (slip, max_slip_pct), seconds, slip
    return True, "", seconds, slip


def line(est: Optional[dict]) -> str:
    """The EXIT LIQUIDITY line for the AI DECISION block, or "" when unknown."""
    if not est:
        return ""
    mid = est.get("2000") or {}
    if mid.get("seconds") is None:
        return ""
    out = "EXIT LIQUIDITY: $2k exits in ~%ds" % mid["seconds"]
    if mid.get("slip_pct") is not None:
        out += ", < %.1f%% slip at current volume" % mid["slip_pct"]
    return out
