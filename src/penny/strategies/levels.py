"""The risk layer: structural levels, deterministic stops, exit rules (Spec 4).

The old engine printed a stop and two percentage targets -- ``T1 4.10 | T2
4.57`` -- on every card, including cards where there was no setup at all. A
target pulled from a fixed percentage is a ghost: nobody is selling there, so it
is not *why* a move stops, and it reads as a promise the tape never made.

This module replaces the ghost levels with things that are actually there:

  - Overhead resistance (Spec 4.2): real levels above the price -- the session
    high, prior highs, consolidation pivots, whole/half-dollar marks. Not a
    target: "here is supply."
  - A structural stop (Spec 4.3): the low the setup is built on, minus a tick.
    Continuation uses the entry bar's low; reversal uses the flush bar's low.
    Fixed percentage stops are gone. A stop wider than the cap is refused with
    STOP_TOO_WIDE.
  - Exit rules (Spec 4.4): an early-failure cut and a structural trailing stop.

And, above all, level suppression (Spec 4.1): when there is no trade -- SETUP
NONE, VERDICT REJECT/PASS, or a BLOCKED gate -- the plan is ``None``. A number
is only shown when it is a level someone would act on.

Everything here is pure: numbers in, numbers out. It reads no clock, no network
and no config (the caps are arguments with sensible defaults).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .indicators import overhead_levels
from .confirmation import CONTINUATION, REVERSAL

# Deterministic stop caps (Spec 4.3): a stop wider than this is not a stop, it
# is a hope, so the setup is refused instead.
CONT_STOP_MAX_PCT = 15.0
REVERSAL_STOP_MAX_PCT = 30.0

# One tick under the structural low.
TICK = 0.01

# ATR buffer for the trailing stop (Spec 4.4), in ATRs.
ATR_TRAIL_MULT = 0.5


@dataclass
class StructuralLevels:
    """The levels a card may show, or None when there is no trade (Spec 4.1).

    ``overhead`` is a list of real resistance levels above the price, ascending.
    It replaces T1/T2: it is supply, not a target.
    """

    mode: str                     # CONTINUATION | REVERSAL
    entry: float
    stop: float
    risk_per_share: float
    shares: int
    overhead: list[float] = field(default_factory=list)
    viable: bool = True
    reason: str = ""              # why not viable, when it is not

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "entry": round(self.entry, 4),
            "stop": round(self.stop, 4),
            "risk_per_share": round(self.risk_per_share, 4),
            "shares": self.shares,
            "overhead": [round(v, 4) for v in self.overhead],
            "viable": self.viable,
            "reason": self.reason,
        }

    def text(self, risk_usd: float = 50.0) -> str:
        over = ", ".join(f"{v:.4f}" for v in self.overhead) or "none overhead"
        out = (f"{self.mode} plan | entry {self.entry:.4f} | "
               f"stop (structural) {self.stop:.4f} | risk/share "
               f"{self.risk_per_share:.4f} | {self.shares} shares "
               f"| overhead: {over} | trail 1m higher-lows, no fixed targets")
        if not self.viable:
            out += f" | NO TRADE: {self.reason}"
        return out


def structural_stop(bars: Sequence[dict], mode: str) -> Optional[float]:
    """The deterministic stop from closed 1m bars (Spec 4.3).

    CONTINUATION -> the last closed bar's low minus a tick.
    REVERSAL     -> the flush bar's low (bar[-2]) minus a tick.

    None when the relevant bar is missing -- a stop with no bar under it is not
    structural, so the caller must refuse the trade rather than invent one.
    """
    if not bars:
        return None
    try:
        if mode == REVERSAL:
            if len(bars) < 2:
                return None
            low = float(bars[-2]["l"])
        else:
            low = float(bars[-1]["l"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if low <= 0:
        return None
    return low - TICK


def stop_ok(entry: float, stop: float, mode: str,
            cont_max_pct: float = CONT_STOP_MAX_PCT,
            reversal_max_pct: float = REVERSAL_STOP_MAX_PCT) -> tuple[bool, str]:
    """Is the stop within the mode's cap? (Spec 4.3 STOP_TOO_WIDE.)"""
    if entry <= 0 or stop <= 0 or stop >= entry:
        return False, "STOP_TOO_WIDE"
    pct = (entry - stop) / entry * 100.0
    cap = reversal_max_pct if mode == REVERSAL else cont_max_pct
    if pct > cap:
        return False, "STOP_TOO_WIDE"
    return True, ""


def plan_from_bars(*, price: float, bars: Sequence[dict], mode: str,
                   day_high: Optional[float] = None,
                   prior_highs: Optional[Sequence[float]] = None,
                   pivots: Optional[Sequence[float]] = None,
                   risk_usd: float = 50.0,
                   suppression: bool = False) -> Optional[StructuralLevels]:
    """A structural plan, or None when levels must be suppressed (Spec 4.1/4.3).

    Returns None (no levels at all) when:
      - ``suppression`` is set -- the caller has already decided there is no
        trade (SETUP NONE / REJECT / PASS / BLOCKED), so no number is shown;
      - the price is not usable;
      - the structural stop cannot be derived; or
      - the stop is wider than the mode's cap (STOP_TOO_WIDE).
    """
    if suppression or price <= 0:
        return None
    stop = structural_stop(bars, mode)
    if stop is None:
        return None
    ok, why = stop_ok(price, stop, mode)
    if not ok:
        return None
    risk = price - stop
    shares = max(0, int(risk_usd / risk)) if risk > 0 else 0
    overhead = overhead_levels(price, day_high, prior_highs=prior_highs, pivots=pivots)
    return StructuralLevels(mode=mode, entry=price, stop=stop, risk_per_share=risk,
                            shares=shares, overhead=overhead, viable=True)


def early_failure_exit(entry: float, trigger: float, entry_bar_low: float,
                       closes: Sequence[float]) -> bool:
    """Spec 4.4: within 2 closed 1m bars, a close below the trigger or the entry
    bar low exits immediately. ``closes`` are the closes since entry, oldest first.
    """
    for c in list(closes)[:2]:
        if c < trigger or c < entry_bar_low:
            return True
    return False


def trailing_stop(higher_low: Optional[float], vwap: Optional[float],
                  atr: Optional[float] = None) -> Optional[float]:
    """Spec 4.4: trail behind the higher of the last 1m higher-low and VWAP,
    minus an ATR buffer. None when neither reference exists.
    """
    refs = [v for v in (higher_low, vwap) if v and v > 0]
    if not refs:
        return None
    ref = max(refs)
    buffer = (atr * ATR_TRAIL_MULT) if (atr and atr > 0) else 0.0
    return ref - buffer


def levels_allowed(gate, verdict: Optional[str] = None) -> bool:
    """Spec 4.1: may a level be shown at all?

    False when there is no trade to show them for: a missing/failed gate, a
    BLOCKED or non-passing gate, or a REJECT/PASS/NONE verdict. A level shown on
    a non-trade is the exact bug the spec names.
    """
    if gate is None:
        return False
    if getattr(gate, "blocked_by", None):
        return False
    if not getattr(gate, "passed", False):
        return False
    setup = (getattr(gate, "primary", None) or "").upper()
    if setup in ("NONE", "PASS", "REJECT"):
        return False
    if verdict is not None and str(verdict).upper() in ("REJECT", "PASS", "NO_TRADE"):
        return False
    return True
