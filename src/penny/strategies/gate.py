"""Run every detector, then decide whether this stock is worth an alert.

The gate exists because a screener's top gainers list is mostly noise. Without
it the alert stream is whatever moved most recently, and the actual winner --
the one that kept going -- is indistinguishable from the twenty that faded. The
gate's job is to make the list short enough to read.

Precedence, in order, and each step is a hard stop:

    DATA_CONFLICT  the quote and the verified price disagree
    DILUTION       an offering priced below today's open
    EXHAUSTED      parabolic -- refuse the new long, never propose a short
    no_strategy    nothing passed
    below_min_score the best primary did not clear the bar (raised in the
                    open, lunch and close windows)

`INSUFFICIENT_DATA` never counts as a pass. It means the detector could not read
its input -- usually because the symbol has not been enriched yet -- and it is
reported honestly rather than treated as a quiet yes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from . import detectors, primitives, spread_liquidity
from .base import Bundle, Detector, StrategyGate, StrategyResult

# Name -> detector. Order is cosmetic; the gate scores, not the sequence.
# The last three are the Spec 2.0 mechanisms: they pass only when NO cause was
# named, which is what lets a real tape setup clear the gate with CAUSE_REQUIRED=0.
PRIMARY: dict[str, Detector] = {
    "first_green_day": primitives.first_green_day,
    "low_float_squeeze": primitives.low_float_squeeze,
    "dollar_breakout": primitives.dollar_breakout,
    "catalyst_volume": primitives.catalyst_volume,
    "float_rotation": primitives.float_rotation,
    "vwap_reclaim": primitives.vwap_reclaim,
    "red_to_green": detectors.red_to_green,
    "worg": detectors.worg,
    "bull_flag": detectors.bull_flag,
    "day3_surge": detectors.day3_surge,
    "sympathy_play": detectors.sympathy_play,
    "failed_breakout": primitives.failed_breakout,
    # Tape mechanisms: a cause is a label, not a precondition. These pass only
    # on a no-headline tape (see detectors._no_named_cause), so they complement
    # catalyst_volume instead of competing with it.
    "silent_accumulation": detectors.silent_accumulation,
    "sympathy_move": detectors.sympathy_move,
    "short_squeeze_mechanics": detectors.short_squeeze_mechanics,
}

TRIGGERS: dict[str, Detector] = {
    "first_pullback": detectors.first_pullback,
    "mss_break": detectors.mss_break,
    "liquidity_grab": detectors.liquidity_grab,
    "v_reversal": detectors.v_reversal,
}

BLOCKERS: dict[str, Detector] = {
    "exhaustion_block": detectors.exhaustion_blocker,
}

ALL_PRIMARY_NAMES = tuple(PRIMARY)
ALL_TRIGGER_NAMES = tuple(TRIGGERS)


def time_of_day_adjustment(now_et: datetime, enabled: bool = True) -> float:
    """Extra points the winning primary must clear, by ET session window.

    The open is noisy, lunch is dead, and the last half hour is driven by the
    close rather than by the setup. Power hour and the middle of the day are
    left alone: those are the windows worth trading.
    """
    if not enabled or now_et is None:
        return 0.0
    minutes = now_et.hour * 60 + now_et.minute
    if 9 * 60 + 30 <= minutes < 10 * 60:
        return 10.0                      # opening noise
    if 11 * 60 + 30 <= minutes < 14 * 60:
        return 5.0                       # lunch
    if 15 * 60 + 30 <= minutes < 16 * 60:
        return 10.0                      # close-driven
    return 0.0


def no_new_entries(now_et: Optional[datetime]) -> bool:
    """After 15:50 the close has stopped making new setups."""
    if now_et is None:
        return False
    return now_et.hour * 60 + now_et.minute >= 15 * 60 + 50


def run_detectors(b: Bundle) -> list[StrategyResult]:
    """Every detector, each isolated. A detector that raises must not silence the
    rest -- one malformed bar in one symbol would otherwise empty the gate."""
    results: list[StrategyResult] = []
    for name, fn in list(PRIMARY.items()) + list(TRIGGERS.items()) + list(BLOCKERS.items()):
        kind = "trigger" if name in TRIGGERS else ("blocker" if name in BLOCKERS else "primary")
        try:
            r = fn(b)
        except Exception as exc:  # noqa: BLE001
            from .base import insufficient
            r = insufficient(name, f"detector error: {type(exc).__name__}")
        # A detector may promote itself to a blocker -- catalyst_volume does when
        # it finds a priced offering below the open. Keep that promotion rather
        # than overwriting it with the detector's nominal kind, or the dilution
        # blocker would be reclassified as an ordinary primary and stop blocking.
        if r.verdict != "BLOCK":
            r.kind = kind
        results.append(r)
    return results


def evaluate_gate(b: Bundle, *, min_score: float = 55.0,
                  # Spec 2.0 A: cause is a label, not a gate -- the default no
                  # longer refuses alerts without a named catalyst (dilution
                  # and DATA_CONFLICT still block). Config can restore the old
                  # bar with CAUSE_REQUIRED=1.
                  require_catalyst: bool = False,
                  time_filter: bool = True,
                  spread_max_pct: float = 1.5, min_dollar_vol_15m: float = 300000.0,
                  min_avg_trade_size: float = 200.0, min_trades_15m: int = 30,
                  exit_liq_max_sec: int = 300, exit_liq_max_slip_pct: float = 1.0,
                  exit_liq_enabled: bool = True) -> StrategyGate:
    results = run_detectors(b)

    # Feature 1: the reality gate. Kept out of BLOCKERS because it needs config
    # thresholds rather than defaults, so it is run here with them supplied.
    try:
        reality = spread_liquidity.evaluate(
            b, spread_max_pct=spread_max_pct, min_dollar_vol_15m=min_dollar_vol_15m,
            min_avg_trade_size=min_avg_trade_size, min_trades_15m=min_trades_15m)
    except Exception as exc:  # noqa: BLE001
        from .base import insufficient
        reality = insufficient("spread_liquidity", "detector error: %s" % type(exc).__name__)
    results.append(reality)

    # Feature 10: the exit must exist. Deliberately redundant with the reality
    # gate above -- belt and braces, because a stock can pass every volume test
    # and still have too little depth to get out of a real position.
    if exit_liq_enabled:
        from ..context import liquidity
        ok, why, _seconds, _slip = liquidity.passes(
            b.bars_1m, b.price, max_seconds=exit_liq_max_sec,
            max_slip_pct=exit_liq_max_slip_pct)
        if not ok:
            from .base import blocked
            results.append(blocked("exit_liquidity", [why],
                                   {"max_seconds": exit_liq_max_sec,
                                    "max_slip_pct": exit_liq_max_slip_pct}))
            return StrategyGate(passed=False, strategies=results, blocked_by="ILLIQUID")

    if b.data_conflict:
        return StrategyGate(passed=False, strategies=results, blocked_by="DATA_CONFLICT")

    # A priced offering below the open is a hard long blocker, checked before any
    # scoring: no setup is worth taking into a dilutive print. Keyed on the marker
    # rather than on `kind`, because any detector may raise it -- offerings show
    # up in the news, which is where catalyst_volume sees them.
    if any("DILUTION" in r.failed_on for r in results):
        return StrategyGate(passed=False, strategies=results, blocked_by="DILUTION")

    # Spec 1: the SEC toxicity firewall. An offering, a reverse split or a
    # resale overhang ends the alert before any scoring or any AI call -- the
    # gate's verdict is what stands between the screener and Telegram, so this
    # is where a veto has to live to be fail-closed. An ACTIVE ATM program does
    # not veto; it tags the card [ACTIVE_ATM] and halves sizing.
    warnings: list[str] = []
    tox_today = getattr(b.now_et, "date", lambda: None)()
    if not tox_today:
        from datetime import date as _date
        tox_today = _date.today()
    if b.filings:
        from . import toxicity
        tox = toxicity.classify(b.filings, today=tox_today)
        if tox.veto:
            tox_blocked = StrategyGate(passed=False, strategies=results,
                                       blocked_by=tox.reason_code,
                                       warnings=[tox.reason_code])
            return tox_blocked
        if tox.elevated:
            warnings.append("ACTIVE_ATM")

    # Blockers are ordered, not merged. The reality gate is checked before
    # exhaustion because an illiquid, wide-spread stock is untakeable regardless
    # of how good the shape looks, and naming it ILLIQUID tells the reader the
    # move was not the problem. There is deliberately no short branch here.
    reality = next((r for r in results
                    if r.name == "spread_liquidity" and r.verdict == "BLOCK"), None)
    if reality is not None:
        reasons = " ".join(reality.reasons or ())
        label = "WIDE_SPREAD" if "spread" in reasons else "ILLIQUID"
        return StrategyGate(passed=False, strategies=results, blocked_by=label,
                            warnings=warnings)

    # Everything else that blocks is exhaustion: the move is spent, so refuse the
    # new long.
    blockers = [r for r in results if r.kind == "blocker" and r.verdict == "BLOCK"]
    if blockers:
        return StrategyGate(passed=False, strategies=results, blocked_by="EXHAUSTED",
                            warnings=warnings)

    primaries = [r for r in results if r.kind == "primary" and r.verdict == "PASS"]
    triggers = [r for r in results if r.kind == "trigger" and r.verdict == "TRIGGER"]

    if not primaries:
        return StrategyGate(passed=False, strategies=results, blocked_by="no_strategy",
                            warnings=warnings)

    # A trigger alone never passes. It only names a better entry on a setup that
    # already qualified.
    primary = max(primaries, key=lambda r: r.score)
    entry_trigger = max(triggers, key=lambda r: r.score) if triggers else None

    if require_catalyst and not any(r.name == "catalyst_volume" and r.verdict == "PASS"
                                    for r in primaries):
        return StrategyGate(passed=False, strategies=results, primary=primary.name,
                            entry_trigger=entry_trigger.name if entry_trigger else None,
                            gate_score=primary.score, blocked_by="no_catalyst",
                            warnings=warnings)

    threshold = min_score + time_of_day_adjustment(b.now_et, time_filter)
    if primary.score < threshold:
        return StrategyGate(passed=False, strategies=results, primary=primary.name,
                            entry_trigger=entry_trigger.name if entry_trigger else None,
                            gate_score=primary.score, blocked_by="below_min_score",
                            warnings=warnings)

    return StrategyGate(passed=True, strategies=results, primary=primary.name,
                        entry_trigger=entry_trigger.name if entry_trigger else None,
                        gate_score=primary.score, blocked_by=None,
                        warnings=warnings)


def gate_summary_lines(gate: StrategyGate, limit: int = 8) -> list[str]:
    """The STRATEGY block for an alert: what fired, and what did not.

    Failures are shown on purpose. "dollar breakout FAIL, prior close 1.28 not
    < 1.00" is what tells the reader the gate actually examined this stock.
    """
    passing = sorted(gate.passing(), key=lambda r: -r.score)[:limit]
    triggers = sorted(gate.triggers(), key=lambda r: -r.score)[:2]
    lines: list[str] = []
    for r in passing:
        reason = f"  {r.reasons[0]}" if r.reasons else ""
        lines.append(f"{r.name:<20} PASS {r.score:>3.0f}{reason}")
    for r in triggers:
        reason = f"  {r.reasons[0]}" if r.reasons else ""
        lines.append(f"{r.name:<20} TRIGGER {r.score:>3.0f}{reason}")
    if gate.blocked_by:
        lines.append(f"blocked: {gate.blocked_by}")
    for w in getattr(gate, "warnings", ()) or ():
        lines.append(f"flagged: {w}")
    return lines
