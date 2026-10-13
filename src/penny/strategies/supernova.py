"""Supernova phase classification (Spec 2.3, 2.4).

Every symbol is in exactly one supernova phase at any moment. The phase names
the shape of the move, and everything downstream -- the confirmation state
machine, the risk layer, the alert card -- reads it rather than re-deriving the
shape from raw numbers.

    PRE_BREAKOUT     tight consolidation beneath a breakout level, no breakout
    FRONT_SIDE       trending up above VWAP on rising volume, higher lows
    PARABOLIC        vertical extension (>35% above 1m EMA9 or >45% above VWAP)
    BLOW_OFF_CRASH   volume climax / first lower-high with a loss of VWAP
    BACK_SIDE        fading: lower highs, volume drying up, >15% below the HOD
    FORMER_SUPERNOVA days/weeks after the run, consolidating for a possible leg 2

Two rules make this safe to trust:

  - Exactly one phase. The classifier is a total function with a fallback, so it
    never returns "unknown" and never returns two answers.
  - Telemetry guard (Spec 2.4). BACK_SIDE means volume is drying up and price is
    coming off the high, so a BACK_SIDE reading that also claims accelerating
    volume is a contradiction. The guard resolves it in favour of the tape and
    never lets the card print "accelerating" on a fading phase.

The classifier is pure: the same inputs always produce the same phase, it reads
nothing from the network, and it runs in well under a millisecond. It is called
from the wiring layer (once per gate evaluation) and carried on
`Bundle.supernova_phase`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .indicators import closes as _closes
from .indicators import ema as _ema

# Phase names. Exactly one is active.
PRE_BREAKOUT = "PRE_BREAKOUT"
FRONT_SIDE = "FRONT_SIDE"
PARABOLIC = "PARABOLIC"
BLOW_OFF_CRASH = "BLOW_OFF_CRASH"
BACK_SIDE = "BACK_SIDE"
FORMER_SUPERNOVA = "FORMER_SUPERNOVA"

PHASES = (PRE_BREAKOUT, FRONT_SIDE, PARABOLIC, BLOW_OFF_CRASH, BACK_SIDE,
          FORMER_SUPERNOVA)

# Phases a fresh long is refused from. The state machine will not arm them and
# the card labels them as danger.
DANGEROUS_PHASES = (BLOW_OFF_CRASH, BACK_SIDE, FORMER_SUPERNOVA)

# -- module thresholds (Spec 2.3), all overridable per call ------------------

# PARABOLIC: vertical extension. The spec names two independent measures; either
# one qualifies.
EMA9_EXT_PCT = 35.0          # % above the 1m EMA9
VWAP_EXT_PCT = 45.0          # % above VWAP
# BACK_SIDE: fading, this far below the high of day.
BACK_SIDE_BELOW_HOD_PCT = 15.0
# BLOW_OFF_CRASH: a giveback this sharp off the high, with VWAP lost.
BLOW_OFF_GIVEBACK_PCT = 6.0
# FRONT_SIDE: near the high on confirming volume.
FRONT_SIDE_VOLX = 2.0
# FORMER_SUPERNOVA: it already ran this much and has given most of it back.
SUPERNOVA_GAIN_PCT = 100.0
FORMER_GIVEBACK_PCT = 35.0
# Acceleration at or above this while off the high is the telemetry conflict.
ACCEL_ALIVE = 1.5


@dataclass
class SupernovaPhase:
    """The classification, with the evidence that produced it."""

    phase: str
    from_high_pct: float          # % off the session high (<=0 = below the high)
    accel: float
    volx_3m: float
    ema9_ext_pct: Optional[float] = None    # % price is above the 1m EMA9
    vwap_ext_pct: Optional[float] = None    # % price is above VWAP
    guard_fired: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def dangerous(self) -> bool:
        """True for phases where a fresh long is refused."""
        return self.phase in DANGEROUS_PHASES

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "from_high_pct": round(self.from_high_pct, 1),
            "accel": round(self.accel, 2),
            "volx_3m": round(self.volx_3m, 2),
            "ema9_ext_pct": None if self.ema9_ext_pct is None else round(self.ema9_ext_pct, 1),
            "vwap_ext_pct": None if self.vwap_ext_pct is None else round(self.vwap_ext_pct, 1),
            "guard_fired": self.guard_fired,
            "reasons": list(self.reasons),
        }


def _ext_pct(price: float, ref: Optional[float]) -> Optional[float]:
    if not ref or ref <= 0 or price <= 0:
        return None
    return (price - ref) / ref * 100.0


def classify(*, price: float, day_high: float, from_high: Optional[float],
             accel: float = 1.0, volx_3m: float = 0.0,
             vwap: Optional[float] = None,
             ema9: Optional[float] = None,
             pct_vs_close: Optional[float] = None,
             minutes_since_low: Optional[float] = None,
             above_vwap: Optional[bool] = None,
             higher_lows: Optional[bool] = None,
             ema9_ext_pct: float = EMA9_EXT_PCT,
             vwap_ext_pct: float = VWAP_EXT_PCT,
             back_side_below_hod_pct: float = BACK_SIDE_BELOW_HOD_PCT,
             blow_off_giveback_pct: float = BLOW_OFF_GIVEBACK_PCT,
             supernova_gain_pct: float = SUPERNOVA_GAIN_PCT,
             former_giveback_pct: float = FORMER_GIVEBACK_PCT,
             front_side_volx: float = FRONT_SIDE_VOLX,
             accel_alive: float = ACCEL_ALIVE) -> SupernovaPhase:
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

    # % off the high: prefer the caller's value (a fraction) over recomputing, so
    # a symbol whose high was recorded on a previous cycle keeps its giveback.
    if from_high is not None:
        from_high_pct = float(from_high) * 100.0
    elif day_high > 0 and price > 0:
        from_high_pct = (price - day_high) / day_high * 100.0
    else:
        from_high_pct = 0.0

    ema_ext = _ext_pct(price, ema9)
    vwap_ext = _ext_pct(price, vwap) if vwap else None
    if above_vwap is None and vwap:
        above_vwap = price >= vwap

    gain_pct = (pct_vs_close or 0.0) * 100.0
    reasons: list[str] = []

    # -- PARABOLIC (Spec 2.3): vertical extension ---------------------------
    # Either measure qualifies: >35% above the 1m EMA9, or >45% above VWAP.
    if ((ema_ext is not None and ema_ext > ema9_ext_pct) or
            (vwap_ext is not None and vwap_ext > vwap_ext_pct)):
        if ema_ext is not None and ema_ext > ema9_ext_pct:
            reasons.append(f"{ema_ext:.0f}% above 1m EMA9")
        if vwap_ext is not None and vwap_ext > vwap_ext_pct:
            reasons.append(f"{vwap_ext:.0f}% above VWAP")
        return SupernovaPhase(PARABOLIC, from_high_pct, accel, volx_3m,
                              ema_ext, vwap_ext, reasons=reasons)

    # -- Telemetry guard (Spec 2.4) -----------------------------------------
    # Off the high but STILL accelerating is the front of a second leg, not a
    # back side. Resolve in favour of the tape: PARABOLIC, flagged.
    if from_high_pct <= -blow_off_giveback_pct and accel >= accel_alive:
        reasons.append(f"off the high {from_high_pct:.1f}% but accel {accel:.2f} still alive")
        return SupernovaPhase(PARABOLIC, from_high_pct, accel, volx_3m,
                              ema_ext, vwap_ext, guard_fired=True, reasons=reasons)

    # -- FORMER_SUPERNOVA ---------------------------------------------------
    # It already ran big and has given most of it back. The move is over.
    if gain_pct >= supernova_gain_pct and from_high_pct <= -former_giveback_pct:
        reasons.append(f"gain {gain_pct:+.0f}% but {from_high_pct:.0f}% off the high")
        return SupernovaPhase(FORMER_SUPERNOVA, from_high_pct, accel, volx_3m,
                              ema_ext, vwap_ext, reasons=reasons)

    # -- BLOW_OFF_CRASH -----------------------------------------------------
    # A sharp giveback with VWAP lost: the blow-off top happened, crash is on.
    if from_high_pct <= -blow_off_giveback_pct and above_vwap is False:
        reasons.append(f"rolled over {from_high_pct:.1f}% off the high, below VWAP")
        return SupernovaPhase(BLOW_OFF_CRASH, from_high_pct, accel, volx_3m,
                              ema_ext, vwap_ext, reasons=reasons)

    # -- BACK_SIDE ----------------------------------------------------------
    # >15% below the high of day: the back side of the arc.
    if from_high_pct <= -back_side_below_hod_pct:
        reasons.append(f"back side, {from_high_pct:.1f}% below HOD")
        return SupernovaPhase(BACK_SIDE, from_high_pct, accel, volx_3m,
                              ema_ext, vwap_ext, reasons=reasons)

    # -- FRONT_SIDE ---------------------------------------------------------
    # Near the high, above VWAP, on confirming volume.
    if above_vwap is not False and volx_3m >= front_side_volx:
        reasons.append(f"above VWAP, volx {volx_3m:.1f}x confirming")
        return SupernovaPhase(FRONT_SIDE, from_high_pct, accel, volx_3m,
                              ema_ext, vwap_ext, reasons=reasons)

    # -- Fallback -----------------------------------------------------------
    # Cannot prove it is anywhere else: it is coiling under the high. The safe
    # default -- PRE_BREAKOUT never arms a chase.
    reasons.append(f"no breakout proven ({from_high_pct:.1f}% off the high)")
    return SupernovaPhase(PRE_BREAKOUT, from_high_pct, accel, volx_3m,
                          ema_ext, vwap_ext, reasons=reasons)


def classify_bundle(b, bars_1m: Optional[Sequence[dict]] = None) -> SupernovaPhase:
    """Classify from a Bundle, pulling the same numbers the live path uses.

    EMA9 is taken from the closed 1m bars when they are present -- an extension
    check needs the bar series, not a single price. Without bars the EMA leg is
    simply not evaluated and the classifier falls back to its other measures.
    """
    if b is None:
        return SupernovaPhase(PRE_BREAKOUT, 0.0, 1.0, 0.0, reasons=["no bundle"])
    bars = bars_1m if bars_1m is not None else getattr(b, "bars_1m", None)
    ema9 = _ema(_closes(bars or []), 9)
    return classify(
        price=getattr(b, "price", 0.0) or 0.0,
        day_high=getattr(b, "day_high", 0.0) or 0.0,
        from_high=getattr(b, "from_high", None),
        accel=float(getattr(b, "accel", 1.0) or 1.0),
        volx_3m=(b.volx_of(3) if hasattr(b, "volx_of") else 0.0),
        vwap=getattr(b, "vwap", None),
        ema9=ema9,
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
