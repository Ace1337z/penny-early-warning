"""Supernova phase classification (Spec 2.3, 2.4).

Every symbol is in exactly one supernova phase at any moment. The phase names
the shape of the move, and everything downstream -- the confirmation state
machine, the telemetry guard, the alert card -- reads it rather than re-deriving
the shape from raw numbers.

    PRE_BREAKOUT     coiling below a level; no breakout yet, volume building
    FRONT_SIDE       breakout done, price still rising, volume confirming
    PARABOLIC        accelerating away from the breakout -- the blow-off leg
    BLOW_OFF_CRASH   blow-off finished, price rolling over; dangerous to chase
    BACK_SIDE        post-parabolic: giving back toward the breakout level
    FORMER_SUPERNOVA the move is spent -- it ran, it faded, it is quiet again

Two rules make this safe to trust:

  - Exactly one phase. The classifier is a total function with a fallback, so
    it never returns "unknown" and never returns two answers.
  - Telemetry guard (Spec 2.4). BACK_SIDE means volume is drying up and price is
    coming off the high, so a BACK_SIDE reading that also claims accelerating
    volume is a contradiction. The guard resolves it in favour of the tape: if
    the price is off the high but the move is still accelerating, the phase is
    PARABOLIC, not BACK_SIDE. A back side never shows accelerating.

The classifier is pure: the same inputs always produce the same phase, it reads
nothing from the network, and it runs in well under a millisecond. It is called
from the wiring layer (once per gate evaluation) and its result is carried on
`Bundle.supernova_phase`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Phase names. Exactly one is active.
PRE_BREAKOUT = "PRE_BREAKOUT"
FRONT_SIDE = "FRONT_SIDE"
PARABOLIC = "PARABOLIC"
BLOW_OFF_CRASH = "BLOW_OFF_CRASH"
BACK_SIDE = "BACK_SIDE"
FORMER_SUPERNOVA = "FORMER_SUPERNOVA"

PHASES = (PRE_BREAKOUT, FRONT_SIDE, PARABOLIC, BLOW_OFF_CRASH, BACK_SIDE,
          FORMER_SUPERNOVA)

# Phases where a fresh long is refused. These are the phases the state machine
# will not arm from and the card labels as danger.
DANGEROUS_PHASES = (BLOW_OFF_CRASH, BACK_SIDE, FORMER_SUPERNOVA)

# -- module thresholds (all overridable per call) ----------------------------

# Within this % of the session high with a still-rising tape -> FRONT_SIDE/
# PARABOLIC. Below (further off the high) the move is rolling over.
NEAR_HIGH_PCT = -6.0
# A big day gain (>= this %) is the signature of a supernova rather than an
# ordinary up day; it gates the FORMER_SUPERNOVA read.
SUPERNOVA_GAIN_PCT = 100.0
# How far off the high, on a supernova that has already faded, counts as
# "spent" rather than merely "back side".
FORMER_GIVEBACK_PCT = -35.0
# Acceleration at or above this while off the high is the telemetry conflict.
ACCEL_ALIVE = 1.5
# Volume multiple (3m) that confirms a front-side push.
FRONT_SIDE_VOLX = 2.0
# Volume multiple (3m) that marks a parabolic push.
PARABOLIC_VOLX = 3.0
# A blow-off crash needs a sharp giveback off the high.
BLOW_OFF_GIVEBACK_PCT = -6.0


@dataclass
class SupernovaPhase:
    """The classification, with the evidence that produced it."""

    phase: str
    # price as a % off the session high (negative = below the high)
    from_high_pct: float
    accel: float
    volx_3m: float
    # True when the telemetry guard overrode a BACK_SIDE reading.
    guard_fired: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def dangerous(self) -> bool:
        """True for phases where a fresh long is discouraged."""
        return self.phase in DANGEROUS_PHASES

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "from_high_pct": round(self.from_high_pct, 1),
            "accel": round(self.accel, 2),
            "volx_3m": round(self.volx_3m, 2),
            "guard_fired": self.guard_fired,
            "reasons": list(self.reasons),
        }


def classify(*, price: float, day_high: float, from_high: Optional[float],
             accel: float = 1.0, volx_3m: float = 0.0,
             pct_vs_close: Optional[float] = None,
             minutes_since_low: Optional[float] = None,
             above_vwap: Optional[bool] = None,
             near_high_pct: float = NEAR_HIGH_PCT,
             supernova_gain_pct: float = SUPERNOVA_GAIN_PCT,
             former_giveback_pct: float = FORMER_GIVEBACK_PCT,
             accel_alive: float = ACCEL_ALIVE,
             front_side_volx: float = FRONT_SIDE_VOLX,
             parabolic_volx: float = PARABOLIC_VOLX,
             blow_off_giveback_pct: float = BLOW_OFF_GIVEBACK_PCT) -> SupernovaPhase:
    """Classify one symbol's supernova phase. Exactly one phase, always.

    All inputs are plain numbers so the classifier is a pure function and both
    the live wiring and the tests feed it the same way. Where a value is missing
    the classifier says less, but it still names a phase (its fallback is
    PRE_BREAKOUT): a silent "unknown" would let a dangerous tape through
    unlabelled.
    """
    price = float(price or 0.0)
    day_high = float(day_high or 0.0)
    accel = float(accel or 1.0)
    volx_3m = float(volx_3m or 0.0)

    # % off the high: prefer the caller's value (a fraction) over recomputing
    # from price/day_high, so a symbol whose high was recorded on a previous
    # cycle keeps its true giveback.
    if from_high is not None:
        from_high_pct = float(from_high) * 100.0
    elif day_high > 0 and price > 0:
        from_high_pct = (price - day_high) / day_high * 100.0
    else:
        from_high_pct = 0.0

    reasons: list[str] = []
    gain_pct = (pct_vs_close or 0.0) * 100.0

    # -- FORMER_SUPERNOVA ---------------------------------------------------
    # It already ran big and it has given most of it back. Neither a
    # continuation nor a reversal candidate: the move is over.
    if gain_pct >= supernova_gain_pct and from_high_pct <= former_giveback_pct:
        reasons.append(f"gain {gain_pct:+.0f}% but {from_high_pct:.0f}% off the high")
        return SupernovaPhase(FORMER_SUPERNOVA, from_high_pct, accel, volx_3m,
                              reasons=reasons)

    # -- Telemetry guard (Spec 2.4) -----------------------------------------
    # A reading that is off the high but STILL accelerating is the front of a
    # second leg, not a back side. Resolve in favour of the tape: PARABOLIC.
    if from_high_pct <= near_high_pct and accel >= accel_alive:
        reasons.append(
            f"off the high {from_high_pct:.1f}% but accel {accel:.2f} -> still alive")
        return SupernovaPhase(PARABOLIC, from_high_pct, accel, volx_3m,
                              guard_fired=True, reasons=reasons)

    # -- BLOW_OFF_CRASH -----------------------------------------------------
    # Sharp giveback off the high, tape no longer accelerating, price under
    # VWAP: the blow-off top already happened and the crash is in progress.
    if from_high_pct <= blow_off_giveback_pct and above_vwap is False:
        reasons.append(f"rolled over {from_high_pct:.1f}% off the high, below VWAP")
        return SupernovaPhase(BLOW_OFF_CRASH, from_high_pct, accel, volx_3m,
                              reasons=reasons)

    # -- BACK_SIDE ----------------------------------------------------------
    # Mid-giveback, off the high but not crashed: the back side of the arc.
    if from_high_pct <= near_high_pct:
        reasons.append(f"back side, {from_high_pct:.1f}% off the high")
        return SupernovaPhase(BACK_SIDE, from_high_pct, accel, volx_3m,
                              reasons=reasons)

    # -- PARABOLIC ----------------------------------------------------------
    # Near the high and pushing on heavy, accelerating volume.
    if volx_3m >= parabolic_volx and accel >= accel_alive:
        reasons.append(f"near high {from_high_pct:.1f}%, volx {volx_3m:.1f}x, "
                       f"accel {accel:.2f}")
        return SupernovaPhase(PARABOLIC, from_high_pct, accel, volx_3m,
                              reasons=reasons)

    # -- FRONT_SIDE ---------------------------------------------------------
    # Near the high, volume confirming, tape not yet vertical.
    if from_high_pct > near_high_pct and volx_3m >= front_side_volx:
        reasons.append(f"near high {from_high_pct:.1f}%, volx {volx_3m:.1f}x confirming")
        return SupernovaPhase(FRONT_SIDE, from_high_pct, accel, volx_3m,
                              reasons=reasons)

    # -- Fallback -----------------------------------------------------------
    # Cannot prove it is anywhere else: it is coiling under the high. This is
    # the safe default -- PRE_BREAKOUT never arms a chase.
    reasons.append(f"no breakout proven ({from_high_pct:.1f}% off the high)")
    return SupernovaPhase(PRE_BREAKOUT, from_high_pct, accel, volx_3m,
                          reasons=reasons)


def classify_bundle(b) -> SupernovaPhase:
    """Classify from a Bundle, pulling the same numbers the live path uses.

    Kept here so nothing else has to know the Bundle's field spellings. A
    Bundle that has no bars yet is still classified -- the phase comes from
    price and volume shape, not from candles.
    """
    if b is None:
        return SupernovaPhase(PRE_BREAKOUT, 0.0, 1.0, 0.0, reasons=["no bundle"])
    accel = float(getattr(b, "accel", 1.0) or 1.0)
    return classify(
        price=getattr(b, "price", 0.0) or 0.0,
        day_high=getattr(b, "day_high", 0.0) or 0.0,
        from_high=getattr(b, "from_high", None),
        accel=accel,
        volx_3m=(b.volx_of(3) if hasattr(b, "volx_of") else 0.0),
        pct_vs_close=getattr(b, "pct_vs_close", None),
        minutes_since_low=getattr(b, "minutes_since_low", None),
        above_vwap=(getattr(b, "above_vwap", None) if hasattr(b, "above_vwap") else None),
    )


def telemetry_guard(phase: str, accel: float) -> tuple[str, bool]:
    """Force telemetry consistency for display (Spec 2.4).

    Returns ``(label, corrected)``. A BACK_SIDE (or BLOW_OFF_CRASH /
    FORMER_SUPERNOVA) phase can never be shown as accelerating; if the raw
    acceleration value would print that way, the label is downgraded and
    ``corrected=True`` so the caller can note the correction.
    """
    accel = float(accel or 1.0)
    if phase in DANGEROUS_PHASES and accel >= ACCEL_ALIVE:
        return "fading", True
    return ("accelerating" if accel >= ACCEL_ALIVE else "steady"), False
