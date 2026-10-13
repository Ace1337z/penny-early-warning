"""Two-stage ARMED -> ENTER confirmation (Spec Section 3).

A setup is not an entry. Stage 1 says "this could be a long"; Stage 2 says "the
tape just agreed". Splitting them is what stops the bot from chasing a sign it
has not seen yet.

  Stage 1  ARMED   the pattern qualified. The setup is remembered with the level
                   that would confirm it and the mode it is traded in. No order,
                   no actionable alert.
  Stage 2  ENTER   a CLOSED 1m bar confirmed (primitives A/B/C below) while the
                   price stayed inside the chase tolerance.

The four outcomes are ARMED, ENTER, NO_TRADE and REJECT. There is no WAIT:
"wait" is a non-decision, and this module exists to turn it into one of the four.

Confirmation primitives, all read off CLOSED 1m bars (the forming bar is dropped
-- a confirmation that has not closed is not a confirmation). Spec 3.1:

  A  volume reversal bar   bar[-1].volume >= 2.0x the 5-bar average, close > open
                           and the close in the upper 50% of the bar's range.
  B  higher-low structure  bar[-1].low > bar[-2].low.
  C  trigger break         bar[-1].close > trigger, price within the 4% chase cap.

Any one primitive confirms; the strongest present is reported.

CONTINUATION vs REVERSAL (Spec 3.2) is decided once, at arming, and never flips:

  CONTINUATION  gap_and_go / vwap_hold / first_pullback. Only on FRONT_SIDE
                holding VWAP. Hard-vetoed when the ticker is BACK_SIDE or has
                dropped >10% off the HOD in under 5 minutes.
  REVERSAL      morning_panic_dip_buy / v_reversal. Requires a completed flush
                bar (bar[-2]) and a confirmed green reversal bar (bar[-1]); never
                fires on the first red dump bar.

A single ArmedState carries exactly one mode, and the confirmation tests only the
structure that matches it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from .candles import clean_bars
from .indicators import range_position

# Never chase more than this % above the armed trigger.
CHASE_PCT = 4.0
# An armed setup that never confirms is abandoned rather than held forever.
ARMED_TTL_S = 600.0

# Primitive A thresholds (Spec 3.1).
VOL_REVERSAL_MULT = 2.0      # bar volume vs the 5-bar average
VOL_REVERSAL_LOOKBACK = 5
RANGE_POSITION_MIN = 0.5     # close must be in the upper half of the bar

# Continuation hard-veto (Spec 3.2).
CONT_DROP_OFF_HOD = 10.0     # % off the HOD ...
CONT_DROP_WINDOW_S = 300.0   # ... within this many seconds

CONTINUATION = "CONTINUATION"
REVERSAL = "REVERSAL"

# Trigger names -> mode (Spec 3.2).
CONTINUATION_TRIGGERS = frozenset({"gap_and_go", "vwap_hold", "first_pullback"})
REVERSAL_TRIGGERS = frozenset({"morning_panic_dip_buy", "v_reversal"})

# Every detector's mode, decided once (Spec 3.2). Continuation buys strength
# holding VWAP; reversal buys a capitulation flush turning back up. A name not in
# this table is a continuation, because a breakout that cannot be shown to be a
# reversal must not be routed as one.
REVERSAL_NAMES = frozenset({
    "v_reversal", "liquidity_grab", "failed_breakout", "red_to_green",
    "morning_panic_dip_buy",
})
CONTINUATION_NAMES = frozenset({
    "gap_and_go", "vwap_hold", "first_pullback", "mss_break", "bull_flag",
    "worg", "day3_surge", "dollar_breakout", "catalyst_volume", "sympathy_play",
    "silent_accumulation", "sympathy_move", "short_squeeze_mechanics",
})


def mode_for_name(name: Optional[str]) -> str:
    """The mode a detector name trades in. Reversal only when named; else CONTINUATION."""
    return REVERSAL if name in REVERSAL_NAMES else CONTINUATION


def mode_for_trigger_or_name(trigger: Optional[str], primary: Optional[str]) -> str:
    """Mode from the setup that actually fired.

    The trigger names the entry mechanic when one fired; otherwise the primary
    does. Exactly one mode is returned.
    """
    if trigger:
        if trigger in REVERSAL_TRIGGERS or trigger in REVERSAL_NAMES:
            return REVERSAL
        return CONTINUATION
    return mode_for_name(primary)


# Phases a fresh long may be armed from. PARABOLIC is the chase-by-definition
# phase and the state machine is the anti-chase.
ARMABLE_PHASES = frozenset({"PRE_BREAKOUT", "FRONT_SIDE"})

EntryState = Literal["ARMED", "ENTER", "NO_TRADE", "REJECT"]


@dataclass
class ArmedState:
    """Stage 1 output: a qualified setup remembered, not an entry."""

    symbol: str
    ts: float                 # epoch seconds when armed
    price: float              # price at arming (the chase reference)
    trigger: float            # level a closed 1m bar must confirm above
    phase: str                # supernova phase at arming
    mode: str                 # CONTINUATION | REVERSAL -- exactly one
    from_high: Optional[float] = None
    day_high: Optional[float] = None
    hod_ts: Optional[float] = None   # when the HOD was set (continuation veto)

    @property
    def trade_class(self) -> str:
        """Back-compat alias: the mode is the trade class."""
        return self.mode

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "ts": self.ts, "price": round(self.price, 4),
                "trigger": round(self.trigger, 4), "phase": self.phase,
                "mode": self.mode, "trade_class": self.mode}


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


def mode_for(trigger: Optional[str], primary: Optional[str] = None) -> str:
    """Decide CONTINUATION xor REVERSAL from the setup's own mechanic.

    The trigger, when one fired, names the mode outright. With no trigger, the
    primary's registered mode decides (Spec 3.2), and an unregistered primary
    defaults to CONTINUATION. Exactly one is returned; there is no path that
    returns both or neither.
    """
    return mode_for_trigger_or_name(trigger, primary)


# Back-compat alias (earlier callers used trade_class_for(gate)). Prefers the
# gate's own recorded mode (Spec 3.2) and only falls back to re-deriving it.
def trade_class_for(gate) -> str:
    recorded = getattr(gate, "confirmation_mode", None)
    if recorded in (CONTINUATION, REVERSAL):
        return recorded
    return mode_for(getattr(gate, "entry_trigger", None), getattr(gate, "primary", None))


def continuation_veto(armed: ArmedState, bundle, now_ts: float) -> Optional[str]:
    """The continuation hard-veto (Spec 3.2 + 3.3), or None when it may proceed.

    A continuation trade is refused when the name is already BACK_SIDE, when it
    has dropped more than 10% off the HOD inside 5 minutes -- both are the
    falling-knife case a continuation must never buy -- or (Spec 3.3) when it is
    a Tier 3 setup (no real driver) after the 10:30 ET cutoff, where a pure-tape
    breakout is a chase into lunch rather than a trade.
    """
    phase = getattr(bundle, "supernova_phase", None)
    if phase == "BACK_SIDE":
        return "BACK_SIDE"
    fh = getattr(bundle, "from_high", None)
    if fh is not None and fh * 100.0 <= -CONT_DROP_OFF_HOD:
        hod_ts = getattr(armed, "hod_ts", None)
        if hod_ts is None or (now_ts - hod_ts) <= CONT_DROP_WINDOW_S:
            return f"dropped {abs(fh) * 100:.0f}% off HOD in under 5m"
    # Spec 3.3: Tier 3 continuation after 10:30 ET is refused.
    tier = getattr(bundle, "catalyst_tier", None)
    if tier == 3:
        from .routing import continuation_allowed
        now_et = getattr(bundle, "now_et", None)
        if not continuation_allowed(tier, now_et, CONTINUATION):
            return "Tier 3 continuation after 10:30 ET"
    return None


def arm(bundle, gate, *, phase: str, now_ts: Optional[float] = None,
        mode: Optional[str] = None) -> Optional[ArmedState]:
    """Stage 1. Return the armed setup, or None when nothing may be armed.

    Nothing arms from a dangerous phase. The mode is the gate's own mechanic
    unless the caller overrides it. The trigger level is the session high -- the
    breakout being waited on -- falling back to a hair above the current price.
    """
    if bundle is None or phase not in ARMABLE_PHASES:
        return None
    price = float(getattr(bundle, "price", 0.0) or 0.0)
    if price <= 0:
        return None
    day_high = float(getattr(bundle, "day_high", 0.0) or 0.0)
    trigger = day_high * 1.001 if day_high > 0 else price * 1.001
    if now_ts is None:
        now = getattr(bundle, "now_et", None)
        now_ts = now.timestamp() if now is not None and hasattr(now, "timestamp") else 0.0
    if mode is None:
        mode = trade_class_for(gate)
    return ArmedState(symbol=getattr(bundle, "symbol", ""), ts=float(now_ts),
                      price=price, trigger=trigger, phase=phase, mode=mode,
                      from_high=getattr(bundle, "from_high", None),
                      day_high=day_high or None,
                      hod_ts=getattr(bundle, "hod_ts", None))


def _closed_bars(bars_1m) -> list[dict]:
    """Parse bars and drop the forming one. Only closed bars may confirm."""
    cleaned = clean_bars(bars_1m)
    if len(cleaned) < 2:
        # A single bar IS the forming bar; with nothing closed, nothing confirms.
        return []
    return cleaned[:-1]


def _primitive_a(bar: dict, bars: list[dict]) -> Optional[str]:
    """Volume reversal bar: >=2x the 5-bar average, green, close in upper half."""
    try:
        o, c, v = float(bar["o"]), float(bar["c"]), float(bar["v"])
    except (KeyError, TypeError, ValueError):
        return None
    if c <= o:
        return None
    look = bars[-VOL_REVERSAL_LOOKBACK:]
    if len(look) < 2:
        return None
    avg = sum(float(b.get("v", 0.0) or 0.0) for b in look) / len(look)
    if avg <= 0 or v < VOL_REVERSAL_MULT * avg:
        return None
    pos = range_position(bar)
    if pos is None or pos < RANGE_POSITION_MIN:
        return None
    return "A"


def _primitive_b(bars: list[dict]) -> Optional[str]:
    """Higher-low structure: the last closed low is above the prior low."""
    if len(bars) < 2:
        return None
    try:
        last_low = float(bars[-1]["l"])
        prev_low = float(bars[-2]["l"])
    except (KeyError, TypeError, ValueError):
        return None
    return "B" if last_low > prev_low else None


def _primitive_c(bar: dict, trigger: float, chase_pct: float,
                 armed_price: float) -> Optional[str]:
    """Trigger break: the bar closed above the trigger, inside the chase cap."""
    try:
        c = float(bar["c"])
    except (KeyError, TypeError, ValueError):
        return None
    if c <= trigger:
        return None
    if armed_price > 0 and (c - armed_price) / armed_price * 100.0 > chase_pct:
        return None
    return "C"


def confirm(armed: Optional[ArmedState], bundle, *, now_ts: Optional[float] = None,
            chase_pct: float = CHASE_PCT, ttl_s: float = ARMED_TTL_S) -> Confirmation:
    """Stage 2. Confirm ENTER from the last closed 1m bar, or report why not.

    Order is deliberate: TTL first (a stale setup is dead whatever the tape
    says), then the mode's hard-veto, then the chase cap, then the primitives.
    """
    if armed is None:
        return Confirmation(state="REJECT", reasons=["no armed setup"])

    ts = now_ts
    if ts is None:
        now = getattr(bundle, "now_et", None)
        ts = now.timestamp() if now is not None and hasattr(now, "timestamp") else armed.ts

    if ttl_s and ts - armed.ts > ttl_s:
        return Confirmation(state="NO_TRADE", reasons=[f"expired after {int(ts - armed.ts)}s"])

    price = float(getattr(bundle, "price", 0.0) or 0.0)
    if price <= 0:
        return Confirmation(state="ARMED", reasons=["no price"])

    # CONTINUATION hard-veto (Spec 3.2) before anything else structural.
    if armed.mode == CONTINUATION:
        veto = continuation_veto(armed, bundle, ts)
        if veto:
            return Confirmation(state="NO_TRADE", reasons=[f"continuation vetoed: {veto}"])

    chase = abs(price - armed.price) / armed.price * 100.0 if armed.price > 0 else 0.0
    if chase > chase_pct:
        return Confirmation(state="NO_TRADE", chase_pct=round(chase, 2),
                            reasons=[f"chase {chase:.1f}% > {chase_pct:.0f}% cap"])

    bars = _closed_bars(getattr(bundle, "bars_1m", None))
    if not bars:
        return Confirmation(state="ARMED", chase_pct=round(chase, 2),
                            reasons=["no closed 1m bar yet"])

    last = bars[-1]

    # REVERSAL (Spec 3.2): needs a completed flush then a confirmed green bar.
    if armed.mode == REVERSAL:
        ok, why = _reversal_ready(bars, armed, chase_pct)
        if not ok:
            return Confirmation(state="ARMED", chase_pct=round(chase, 2), reasons=[why])

    # Primitive A (volume reversal bar) is the strongest single confirmation.
    prim_a = _primitive_a(last, bars)
    if prim_a:
        return Confirmation(state="ENTER", primitive="A", chase_pct=round(chase, 2),
                            reasons=["volume reversal bar >=2x, close in upper half"])

    # Primitive C (trigger break) -- priced against the trigger and chase cap.
    prim_c = _primitive_c(last, float(armed.trigger), chase_pct, armed.price)
    if prim_c:
        return Confirmation(state="ENTER", primitive="C", chase_pct=round(chase, 2),
                            reasons=[f"closed {float(last['c']):.4f} above trigger "
                                     f"{float(armed.trigger):.4f}"])

    # Primitive B (higher-low) -- weakest, only if the bar is still constructive.
    prim_b = _primitive_b(bars)
    if prim_b and float(last["c"]) >= float(last["o"]):
        return Confirmation(state="ENTER", primitive="B", chase_pct=round(chase, 2),
                            reasons=["higher-low structure held"])

    return Confirmation(state="ARMED", chase_pct=round(chase, 2),
                        reasons=["no confirmation yet"])


def _reversal_ready(bars: list[dict], armed: ArmedState,
                    chase_pct: float) -> tuple[bool, str]:
    """Spec 3.2 reversal precondition: a completed flush, then a green bar.

    bar[-2] must be a capitulation flush (down bar, heavy volume) and bar[-1] a
    confirmed green reversal bar. The first red dump bar never fires.
    """
    if len(bars) < 2:
        return False, "need a flush bar and a reversal bar"
    flush, rev = bars[-2], bars[-1]
    try:
        fo, fc, fv = float(flush["o"]), float(flush["c"]), float(flush["v"])
        ro, rc = float(rev["o"]), float(rev["c"])
    except (KeyError, TypeError, ValueError):
        return False, "malformed bar"
    if fc >= fo:
        return False, "prior bar is not a flush"
    if rc <= ro:
        return False, "reversal bar is not green"
    return True, "flush then green reversal"


# --------------------------------------------------------------------------
# Deterministic-first routing (Spec 5)
# --------------------------------------------------------------------------

# The full outcome vocabulary, in one place. WAIT is not in it.
OUTCOMES = ("ENTER", "ARMED", "NO_TRADE", "REJECT")


def deterministic_verdict(gate, armed: Optional[ArmedState], bundle, *,
                          now_ts: Optional[float] = None) -> str:
    """The rule-engine's verdict, computed with no I/O (Spec 5).

    This is the routing decision the alert is built from: it is a pure function
    of the gate, the armed state and the bundle's closed bars, so it returns in
    well under the 10 ms budget and is identical on every machine. The LLM is a
    *red team* that runs afterwards and can only ever downgrade this -- it never
    gates the alert, because a network call must never sit in the hot path.

    Exactly one of ENTER | ARMED | NO_TRADE | REJECT is returned:

      REJECT    the gate did not pass -- there is no setup to speak of.
      ENTER     a closed 1m bar confirmed the entry (primitives A/B/C).
      ARMED     the setup qualified but the tape has not confirmed it yet.
      NO_TRADE  it was confirmed-then-vetoed, expired, or chased too far.

    A missing armed state on a passing gate is ARMED-worthy only if we have not
    even armed; without an arm there is nothing to confirm, so it is NO_TRADE.
    """
    if gate is None or not getattr(gate, "passed", False):
        return "REJECT"
    if armed is None:
        return "NO_TRADE"
    return confirm(armed, bundle, now_ts=now_ts).state
