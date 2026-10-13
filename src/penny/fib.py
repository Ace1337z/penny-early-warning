"""Fibonacci levels and the trade plan (6.9)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

RETRACEMENTS = (0.236, 0.382, 0.5, 0.618, 0.786)
EXTENSIONS = (1.272, 1.618)


@dataclass
class FibLevels:
    swing_low: float
    swing_high: float
    range: float
    retracements: dict[str, float] = field(default_factory=dict)
    extensions: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "swing_low": round(self.swing_low, 5),
            "swing_high": round(self.swing_high, 5),
            "retracements": {k: round(v, 5) for k, v in self.retracements.items()},
            "extensions": {k: round(v, 5) for k, v in self.extensions.items()},
        }


@dataclass
class TradePlan:
    """The trade plan (Spec 4). Structural levels, never fixed percentages.

    ``target1``/``target2`` are gone (Spec 4.2): a percentage target is a ghost,
    so the plan carries ``overhead`` -- the real resistance levels above the
    entry -- and the reader trails instead of aiming at a number. ``stop`` is a
    structural low, not a percentage.
    """

    mode: str                     # "pullback" | "breakout"
    entry_low: float
    entry_high: float
    entry: float                  # representative entry used for sizing
    stop: float
    risk_per_share: float
    shares: int
    overhead: list[float] = field(default_factory=list)
    reward_risk: Optional[float] = None   # kept for callers; sized off overhead
    viable: bool = True           # False when the stop was too wide
    reason: str = ""              # why not viable (e.g. STOP_TOO_WIDE)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "entry_low": round(self.entry_low, 5),
            "entry_high": round(self.entry_high, 5),
            "entry": round(self.entry, 5),
            "stop": round(self.stop, 5),
            "risk_per_share": round(self.risk_per_share, 5),
            "shares": self.shares,
            "overhead": [round(v, 5) for v in self.overhead],
            "reward_risk": round(self.reward_risk, 2) if self.reward_risk else None,
            "viable": self.viable,
            "reason": self.reason,
        }

    def text(self, risk_usd: float) -> str:
        over = ", ".join(f"{v:.4f}" for v in self.overhead) or "none overhead"
        out = (
            f"{self.mode.upper()} plan | entry {self.entry_low:.4f}-{self.entry_high:.4f} "
            f"| stop (structural) {self.stop:.4f} | risk/share "
            f"{self.risk_per_share:.4f} | {self.shares} shares for ${risk_usd:.0f} risk "
            f"| overhead: {over} | trail 1m higher-lows, no fixed targets"
        )
        if not self.viable:
            out += f" | NO TRADE: {self.reason or 'R:R below minimum'}"
        return out


def fib_levels(swing_low: float, swing_high: float) -> FibLevels:
    rng = swing_high - swing_low
    levels = FibLevels(swing_low=swing_low, swing_high=swing_high, range=rng)
    for ratio in RETRACEMENTS:
        levels.retracements[f"{ratio * 100:.1f}"] = swing_high - ratio * rng
    for ratio in EXTENSIONS:
        levels.extensions[f"{ratio * 100:.1f}"] = swing_low + ratio * rng
    return levels


def trade_plan(price: float, levels: FibLevels, risk_usd: float = 50.0,
               min_rr: Optional[float] = None,
               day_high: Optional[float] = None,
               prior_highs: Optional[list] = None,
               mode: str = "CONTINUATION") -> Optional[TradePlan]:
    """Structural entry/stop/overhead plan (Spec 4). None if the stop is unusable.

    Fixed-percentage targets are gone (Spec 4.2): the plan carries the real
    resistance above the entry (``overhead``) instead of T1/T2. The stop is the
    swing level (Spec 4.3), and a stop wider than the mode's cap is flagged
    STOP_TOO_WIDE and marked not viable -- the numbers still render so the
    reader sees why, but nothing may treat it as a recommendation.
    """
    if price <= 0 or levels.range <= 0:
        return None
    from .strategies.indicators import overhead_levels
    from .strategies.levels import (CONT_STOP_MAX_PCT, REVERSAL_STOP_MAX_PCT,
                                    REVERSAL)

    high = levels.swing_high
    low = levels.swing_low
    rng = levels.range
    r382 = levels.retracements.get("38.2")
    r500 = levels.retracements.get("50.0")
    r618 = levels.retracements.get("61.8")
    if None in (r382, r500, r618):
        return None

    if price <= high - 0.3 * rng:
        leg = "pullback"
        entry_low = min(r382, r500)
        entry_high = max(r382, r500)
        entry = (entry_low + entry_high) / 2.0
        stop = r618 * 0.99
    else:
        leg = "breakout"
        entry_low = high * 1.003
        entry_high = high * 1.010
        entry = (entry_low + entry_high) / 2.0
        stop = r382 * 0.99

    risk = entry - stop
    if risk <= 0:
        return None
    shares = max(0, int(risk_usd / risk))

    # Spec 4.3: the stop must sit inside the mode's cap.
    cap = REVERSAL_STOP_MAX_PCT if mode == REVERSAL else CONT_STOP_MAX_PCT
    stop_pct = (entry - stop) / entry * 100.0
    viable = stop_pct <= cap
    reason = "" if viable else "STOP_TOO_WIDE"

    # Spec 4.2: real overhead supply, not a percentage target.
    overhead = overhead_levels(entry, day_high, prior_highs=prior_highs,
                               extra=[high, r618])

    # R:R is retained for callers that keyed off it, computed against the first
    # real overhead level (the nearest supply) rather than a ghost target.
    first_over = overhead[0] if overhead else None
    rr = ((first_over - entry) / risk) if (first_over and risk > 0) else None

    return TradePlan(
        mode=leg, entry_low=entry_low, entry_high=entry_high, entry=entry, stop=stop,
        risk_per_share=risk, shares=shares, overhead=overhead, reward_risk=rr,
        viable=viable, reason=reason,
    )


def reentry_guidance(price: float, vwap: Optional[float], levels: Optional[FibLevels]) -> str:
    """Re-entry guidance used during tracking (6.9)."""
    bits = []
    if vwap:
        bits.append(f"VWAP {vwap:.4f}")
    if levels:
        r382 = levels.retracements.get("38.2")
        r500 = levels.retracements.get("50.0")
        if r382 and r500:
            bits.append(f"38.2-50% zone {min(r382, r500):.4f}-{max(r382, r500):.4f} on returning volume")
    return "re-entry: " + (", ".join(bits) if bits else "no levels")
