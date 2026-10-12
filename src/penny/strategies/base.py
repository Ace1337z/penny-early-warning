"""Strategy gate: the long-only filter that decides what may reach the AI.

Every detector returns a `StrategyResult`. The gate in `gate.py` collects them
into a `StrategyGate`, which is the only thing standing between the raw
screener and a Telegram alert. Its job is to make the alert list short enough
that the actual winner is not buried under noise.

Three rules are enforced here rather than trusted to each detector:

- `INSUFFICIENT_DATA` is never a PASS. A detector that could not read its input
  says so and is named in `failed_on`; it does not quietly pass.
- A `TRIGGER` never passes the gate on its own. It can only upgrade a primary
  that already passed.
- A `BLOCK` forces `passed=False` no matter how many primaries passed. This is
  where exhaustion and dilution live, and it is why the system is long-only:
  there is no code path that produces a short entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

Verdict = Literal["PASS", "FAIL", "INSUFFICIENT_DATA", "TRIGGER", "BLOCK"]


@dataclass
class StrategyResult:
    name: str
    verdict: Verdict
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    failed_on: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    kind: str = "primary"          # "primary" | "trigger" | "blocker"
    # Soft warnings that never block. They exist so the AI can be told "this is
    # a wide spread" without that turning into a gate rejection: the reader
    # judges a 1.2% spread and an 8% spread differently, but only one of them
    # is untakeable.
    flags: list[str] = field(default_factory=list)

    @property
    def counts_as_pass(self) -> bool:
        return self.verdict == "PASS"

    def as_dict(self) -> dict:
        return {"name": self.name, "verdict": self.verdict, "score": round(self.score, 1),
                "reasons": self.reasons, "failed_on": self.failed_on, "kind": self.kind,
                "flags": self.flags}


@dataclass
class StrategyGate:
    passed: bool
    strategies: list[StrategyResult] = field(default_factory=list)
    primary: Optional[str] = None
    entry_trigger: Optional[str] = None
    gate_score: float = 0.0
    blocked_by: Optional[str] = None

    def by_name(self, name: str) -> Optional[StrategyResult]:
        for r in self.strategies:
            if r.name == name:
                return r
        return None

    def passing(self) -> list[StrategyResult]:
        return [r for r in self.strategies if r.verdict == "PASS"]

    def triggers(self) -> list[StrategyResult]:
        return [r for r in self.strategies if r.verdict == "TRIGGER"]

    def flags(self) -> list[str]:
        """Every soft warning raised by any detector, de-duplicated, in order."""
        out: list[str] = []
        for r in self.strategies:
            for f in getattr(r, "flags", ()) or ():
                if f not in out:
                    out.append(f)
        return out

    def as_dict(self) -> dict:
        return {"passed": self.passed, "primary": self.primary,
                "entry_trigger": self.entry_trigger, "gate_score": round(self.gate_score, 1),
                "blocked_by": self.blocked_by,
                "strategies": [r.as_dict() for r in self.strategies]}


# -- helpers shared by the detectors -----------------------------------------

def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def insufficient(name: str, reason: str, kind: str = "primary") -> StrategyResult:
    """A detector that could not read its input. Never a PASS."""
    return StrategyResult(name=name, verdict="INSUFFICIENT_DATA", kind=kind,
                          failed_on=[reason])


def failed(name: str, reasons: list[str], failed_on: list[str], evidence: dict,
           kind: str = "primary") -> StrategyResult:
    return StrategyResult(name=name, verdict="FAIL", kind=kind,
                          reasons=reasons[:4], failed_on=failed_on[:4], evidence=evidence)


def passed(name: str, score: float, reasons: list[str], evidence: dict,
           kind: str = "primary") -> StrategyResult:
    return StrategyResult(name=name, verdict="PASS", score=clamp(score), kind=kind,
                          reasons=reasons[:4], failed_on=[], evidence=evidence)


def triggered(name: str, score: float, reasons: list[str], evidence: dict) -> StrategyResult:
    return StrategyResult(name=name, verdict="TRIGGER", score=clamp(score), kind="trigger",
                          reasons=reasons[:4], failed_on=[], evidence=evidence)


def blocked(name: str, reasons: list[str], evidence: dict) -> StrategyResult:
    return StrategyResult(name=name, verdict="BLOCK", kind="blocker",
                          reasons=reasons[:4], failed_on=[], evidence=evidence)


def blocked_by_dilution(name: str, reason: str) -> StrategyResult:
    """An offering priced below the open. Carries the marker gate.py looks for."""
    return StrategyResult(name=name, verdict="BLOCK", kind="blocker", reasons=[reason],
                          failed_on=["DILUTION"], evidence={"dilution": True})


# -- the bundle the detectors read -------------------------------------------

@dataclass
class Bundle:
    """Everything a detector may read. Assembled by gate.py, never by a detector.

    Fields the engine already has for every symbol on every cycle are always
    populated. The enrichment-derived ones (candles, news, filings, float) are
    only present for symbols that have been through `Enricher.enrich()`, so a
    detector that needs them must return INSUFFICIENT_DATA otherwise rather
    than guess -- guessing here would invent a setup that did not happen.
    """

    symbol: str
    price: float = 0.0
    prev_close: Optional[float] = None
    open_price: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    cum_volume: float = 0.0
    pct_vs_close: Optional[float] = None
    # Quote quality. Finviz carries no bid/ask, so these stay None on a
    # Finviz-only feed and spread_liquidity reports INSUFFICIENT_DATA rather
    # than guessing a spread from the last print.
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread_pct: Optional[float] = None
    trades_15m: Optional[int] = None
    rise: dict = field(default_factory=dict)
    volx: dict = field(default_factory=dict)
    dollar_vol: dict = field(default_factory=dict)
    vwap: Optional[float] = None
    from_high: Optional[float] = None
    minutes_since_low: Optional[float] = None
    minutes_tracked: float = 0.0
    session: str = "regular"
    now_et: Any = None
    data_conflict: bool = False
    # Thresholds the engine copies from config once per cycle, so a detector
    # never reads config directly (and stays trivially testable).
    dollar_level: float = 1.00
    float_max: float = 20_000_000.0
    # Spec 2.0 E: the short-squeeze floor (was 15).
    short_float_min: float = 8.0
    # How old a headline may be and still be named the cause. Copied from
    # config once per call (same pattern as dollar_level); the module default
    # matches the 24h horizon the rest of the system uses.
    cause_max_age_h: float = 24.0
    sector_leader: Any = None
    # Peer 15m moves as {symbol: pct}, when a peer feed is wired. None means
    # "not fetched" -- sympathy_move reports INSUFFICIENT_DATA, never a guess.
    sector_peers: Optional[dict] = None

    # blocker survives even if that detector did not itself reach a verdict.
    dilution: str = ""
    # Enrichment-derived; None until the symbol has been enriched.
    bars_1m: Optional[list] = None
    news: Optional[list] = None
    filings: Optional[list] = None
    float_shares: Optional[float] = None
    market_cap: Optional[float] = None
    short_pct: Optional[float] = None
    short_ratio: Optional[float] = None
    sector: str = ""
    plan: Any = None
    # Prior completed sessions, oldest first. Empty until history accumulates.
    daily_history: list = field(default_factory=list)

    # -- convenience ------------------------------------------------------
    def rise_of(self, minutes: int) -> float:
        return float(self.rise.get(minutes, 0.0) or 0.0)

    def volx_of(self, minutes: int) -> float:
        return float(self.volx.get(minutes, 0.0) or 0.0)

    def dollars_of(self, minutes: int) -> float:
        return float(self.dollar_vol.get(minutes, 0.0) or 0.0)

    @property
    def above_vwap(self) -> bool:
        return self.vwap is not None and self.price > self.vwap

    @property
    def enriched(self) -> bool:
        return self.bars_1m is not None or self.news is not None

    @property
    def pct(self) -> float:
        return (self.pct_vs_close or 0.0) * 100.0

    def max_gain(self, minutes: int) -> float:
        return self.rise_of(minutes) * 100.0


Detector = Callable[[Bundle], StrategyResult]
