"""Two-stage ARMED -> ENTER confirmation (Spec 2.3, Section 3).

A setup is not an entry. Stage 1 says "this could be a long"; Stage 2 says "the
tape just agreed". Splitting them is what stops the bot from chasing a sign it
has not seen yet.

  Stage 1  ARMED   the gate passed and the phase is actionable. No alert-level
                   decision is made yet; the setup is remembered with the level
                   that would confirm it.
  Stage 2  ENTER   a CLOSED 1m bar confirmed above the armed level (primitives
                   A/B/C below) while the price stayed within the chase cap.

The four possible outcomes are ARMED, ENTER, NO_TRADE and REJECT. There is no
WAIT: "wait" is a non-decision, and this module exists precisely to turn it into
one of the four. ARMED is the only state that keeps listening; NO_TRADE and
REJECT are terminal and the setup is forgotten.

Confirmation primitives, all read off a CLOSED 1m bar (the forming bar is
dropped -- a confirmation that has not closed is not a confirmation):

  A  break & hold   the last closed bar closed above the trigger and its own low
                    held the trigger. The clean breakout.
  B  reclaim        the last closed bar opened below the trigger and closed back
                    above it, having wicked under. The failed break that held.
  C  volume push    A or B, and that bar's volume was at or above the mean of the
                    last three closed bars. A silent breakout is suspect; a loud
                    one is not.

CONTINUATION vs REVERSAL is decided once, at arming, and never flips. A
CONTINUATION holds a move that is already up; a REVERSAL buys the snap-back off
a blow-off. They are mutually exclusive: a single ArmedState carries exactly one
trade_class, and the confirmation only ever tests the level that matches it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from .candles import clean_bars

# Never chase more than this % above the armed price. A setup that ran away is
# not the same trade at a worse price; it is a different trade.
CHASE_PCT = 4.0
# An armed setup that never confirms is abandoned rather than held forever.
ARMED_TTL_S = 600.0

# Trigger names that name a REVERSAL. Everything else is a CONTINUATION. The
# list is short on purpose: a reversal must be proven by the trigger's own
# mechanic, not inferred from a soft agreement.
REVERSAL_TRIGGERS = frozenset({"v_reversal"})
# Primaries whose mechanic is buying a post-drop recovery. Used only when the
# gate supplied no trigger at all.
REVERSAL_PRIMARIES = frozenset({"v_reversal", "liquidity_grab"})

# Phases a fresh long may be armed from. PARABOLIC is excluded: it is the
# chase-by-definition phase and the state machine is the anti-chase.
ARMABLE_PHASES = frozenset({"PRE_BREAKOUT", "FRONT_SIDE"})

EntryState = Literal["ARMED", "ENTER", "NO_TRADE", "REJECT"]

CONTINUATION = "CONTINUATION"
REVERSAL = "REVERSAL"


@dataclass
class ArmedState:
    """Stage 1 output: a setup remembered, not an entry."""

    symbol: str
    ts: float                 # epoch seconds when armed
    price: float              # price at arming (the chase reference)
    trigger: float            # level a closed 1m bar must confirm above
    phase: str                # supernova phase at arming
    trade_class: str          # CONTINUATION | REVERSAL -- exactly one
    from_high: Optional[float] = None

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "ts": self.ts, "price": round(self.price, 4),
                "trigger": round(self.trigger, 4), "phase": self.phase,
                "trade_class": self.trade_class}


@dataclass
class Confirmation:
    """Stage 2 output."""

    state: EntryState
    primitive: Optional[str] = None   # A | B | C | None
    chase_pct: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def entered(self) -> bool:
        return self.state == "ENTER"

    def as_dict(self) -> dict:
        return {"state": self.state, "primitive": self.primitive,
                "chase_pct": round(self.chase_pct, 2), "reasons": list(self.reasons)}


def trade_class_for(gate) -> str:
    """Decide CONTINUATION xor REVERSAL from the gate's winning mechanic.

    The trigger, when one fired, names the class outright. With no trigger, a
    reversal primary names REVERSAL and everything else is CONTINUATION. Exactly
    one is returned; there is no path that returns both or neither.
    """
    trigger = getattr(gate, "entry_trigger", None)
    if trigger:
        return REVERSAL if trigger in REVERSAL_TRIGGERS else CONTINUATION
    primary = getattr(gate, "primary", None)
    if primary in REVERSAL_PRIMARIES:
        return REVERSAL
    return CONTINUATION


def arm(bundle, gate, *, phase: str, now_ts: Optional[float] = None) -> Optional[ArmedState]:
    """Stage 1. Return the armed setup, or None when nothing may be armed.

    Nothing is armed from a dangerous phase (the caller has already seen the
    phase). The trigger level is the session high -- the breakout the setup is
    waiting on -- falling back to a small step above the current price when no
    high is known yet.
    """
    if bundle is None:
        return None
    if phase not in ARMABLE_PHASES:
        return None
    price = float(getattr(bundle, "price", 0.0) or 0.0)
    if price <= 0:
        return None
    day_high = float(getattr(bundle, "day_high", 0.0) or 0.0)
    if day_high > 0:
        # Confirm above the high the setup must clear. A hair above the high
        # avoids confirming on a print that merely equals it.
        trigger = day_high * 1.001
    else:
        trigger = price * 1.001
    if now_ts is None:
        now = getattr(bundle, "now_et", None)
        now_ts = now.timestamp() if now is not None and hasattr(now, "timestamp") else 0.0
    return ArmedState(symbol=getattr(bundle, "symbol", ""), ts=float(now_ts),
                      price=price, trigger=trigger, phase=phase,
                      trade_class=trade_class_for(gate),
                      from_high=getattr(bundle, "from_high", None))


def _closed_bars(bars_1m) -> list[dict]:
    """Parse bars and drop the forming one. Only closed bars may confirm."""
    cleaned = clean_bars(bars_1m)
    if len(cleaned) < 2:
        # A single bar IS the forming bar; with nothing closed, nothing confirms.
        return []
    return cleaned[:-1]


def confirm(armed: Optional[ArmedState], bundle, *, now_ts: Optional[float] = None,
            chase_pct: float = CHASE_PCT, ttl_s: float = ARMED_TTL_S) -> Confirmation:
    """Stage 2. Confirm ENTER from the last closed 1m bar, or report why not.

    Order of checks is deliberate: TTL first (a stale setup is dead whatever the
    tape says), then the chase guard (a runaway price invalidates the setup),
    then the primitives.
    """
    if armed is None:
        return Confirmation(state="REJECT", reasons=["no armed setup"])

    ts = now_ts
    if ts is None:
        now = getattr(bundle, "now_et", None)
        ts = now.timestamp() if now is not None and hasattr(now, "timestamp") else armed.ts

    # TTL: a setup that took too long to confirm is no longer the setup.
    if ttl_s and ts - armed.ts > ttl_s:
        return Confirmation(state="NO_TRADE",
                            reasons=[f"expired after {int(ts - armed.ts)}s"])

    price = float(getattr(bundle, "price", 0.0) or 0.0)
    if price <= 0:
        return Confirmation(state="ARMED", reasons=["no price"])

    chase = abs(price - armed.price) / armed.price * 100.0 if armed.price > 0 else 0.0
    if chase > chase_pct:
        return Confirmation(state="NO_TRADE", chase_pct=round(chase, 2),
                            reasons=[f"chase {chase:.1f}% > {chase_pct:.0f}% cap"])

    bars = _closed_bars(getattr(bundle, "bars_1m", None))
    if not bars:
        return Confirmation(state="ARMED", chase_pct=round(chase, 2),
                            reasons=["no closed 1m bar yet"])

    last = bars[-1]
    o = float(last["o"])
    h = float(last["h"])
    low = float(last["l"])
    c = float(last["c"])
    v = float(last["v"])
    trigger = float(armed.trigger)

    volx_ok = False
    if len(bars) >= 3:
        mean_v = sum(float(b["v"]) for b in bars[-3:]) / 3.0
        volx_ok = mean_v > 0 and v >= mean_v

    # Primitive A: break & hold -- closed above the trigger, low held it.
    if c > trigger and low >= trigger:
        if volx_ok:
            return Confirmation(state="ENTER", primitive="C", chase_pct=round(chase, 2),
                                reasons=[f"break+hold close {c:.4f} above {trigger:.4f} on volume"])
        return Confirmation(state="ENTER", primitive="A", chase_pct=round(chase, 2),
                            reasons=[f"break+hold close {c:.4f} above {trigger:.4f}"])

    # Primitive B: reclaim -- wicked under, opened under, closed back above.
    if low < trigger and o < trigger <= c:
        if volx_ok:
            return Confirmation(state="ENTER", primitive="C", chase_pct=round(chase, 2),
                                reasons=[f"reclaim close {c:.4f} above {trigger:.4f} on volume"])
        return Confirmation(state="ENTER", primitive="B", chase_pct=round(chase, 2),
                            reasons=[f"reclaim close {c:.4f} above {trigger:.4f}"])

    return Confirmation(state="ARMED", chase_pct=round(chase, 2),
                        reasons=[f"no close above trigger {trigger:.4f} "
                                 f"(last close {c:.4f})"])
