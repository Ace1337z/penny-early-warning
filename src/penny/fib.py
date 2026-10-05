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
    mode: str                     # "pullback" | "breakout"
    entry_low: float
    entry_high: float
    entry: float                  # representative entry used for sizing
    stop: float
    target1: float
    target2: float
    risk_per_share: float
    reward_risk: Optional[float]
    shares: int

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "entry_low": round(self.entry_low, 5),
            "entry_high": round(self.entry_high, 5),
            "entry": round(self.entry, 5),
            "stop": round(self.stop, 5),
            "target1": round(self.target1, 5),
            "target2": round(self.target2, 5),
            "risk_per_share": round(self.risk_per_share, 5),
            "reward_risk": round(self.reward_risk, 2) if self.reward_risk else None,
            "shares": self.shares,
        }

    def text(self, risk_usd: float) -> str:
        rr = f"{self.reward_risk:.1f}:1" if self.reward_risk else "n/a"
        return (
            f"{self.mode.upper()} plan | entry {self.entry_low:.4f}-{self.entry_high:.4f} "
            f"| stop {self.stop:.4f} | T1 {self.target1:.4f} | T2 {self.target2:.4f} "
            f"| R:R {rr} | {self.shares} shares for ${risk_usd:.0f} risk"
        )


def fib_levels(swing_low: float, swing_high: float) -> FibLevels:
    rng = swing_high - swing_low
    levels = FibLevels(swing_low=swing_low, swing_high=swing_high, range=rng)
    for ratio in RETRACEMENTS:
        levels.retracements[f"{ratio * 100:.1f}"] = swing_high - ratio * rng
    for ratio in EXTENSIONS:
        levels.extensions[f"{ratio * 100:.1f}"] = swing_low + ratio * rng
    return levels


def trade_plan(price: float, levels: FibLevels, risk_usd: float = 50.0) -> Optional[TradePlan]:
    """Entry/stop/targets from the Fibonacci swing (6.9). None if risk per share <= 0."""
    if price <= 0 or levels.range <= 0:
        return None
    high = levels.swing_high
    low = levels.swing_low
    rng = levels.range
    r382 = levels.retracements.get("38.2")
    r500 = levels.retracements.get("50.0")
    r618 = levels.retracements.get("61.8")
    t1 = levels.extensions.get("127.2")
    t2 = levels.extensions.get("161.8")
    if None in (r382, r500, r618, t1, t2):
        return None

    if price <= high - 0.3 * rng:
        mode = "pullback"
        entry_low = min(r382, r500)
        entry_high = max(r382, r500)
        entry = (entry_low + entry_high) / 2.0
        stop = r618 * 0.99
    else:
        mode = "breakout"
        entry_low = high * 1.003
        entry_high = high * 1.010
        entry = (entry_low + entry_high) / 2.0
        stop = r382 * 0.99

    risk = entry - stop
    if risk <= 0:
        return None
    shares = max(0, int(risk_usd / risk))
    rr = (t1 - entry) / risk if risk > 0 else None
    return TradePlan(
        mode=mode, entry_low=entry_low, entry_high=entry_high, entry=entry, stop=stop,
        target1=t1, target2=t2, risk_per_share=risk, reward_risk=rr, shares=shares,
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
