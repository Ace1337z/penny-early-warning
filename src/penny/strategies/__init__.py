"""The long-only strategy gate.

Eighteen detectors, evaluated per symbol, that decide whether a move is a setup
worth an alert or just a number that went up. The gate is the only thing between
the screener and Telegram: everything that gets through here is something a
trader would recognise by name.

No detector in this package can produce a short entry. Parabolic exhaustion and
dilution are blockers -- they stop a long from being issued, which is the only
long-only-safe way to express them.
"""

from .base import (Bundle, StrategyGate, StrategyResult, Verdict, blocked,
                   blocked_by_dilution, clamp, failed, insufficient, passed, triggered)
from .gate import (ALL_PRIMARY_NAMES, ALL_TRIGGER_NAMES, evaluate_gate, gate_summary_lines,
                   no_new_entries, run_detectors, time_of_day_adjustment)
from .news_cause import Cause, cause_lines, classify, extract_cause, rank_catalysts
from .confirmation import (CONTINUATION, REVERSAL, ArmedState, Confirmation, arm,
                           confirm, mode_for_name, mode_for_trigger_or_name)
from .routing import (TIER_1, TIER_2, TIER_3, catalyst_tier, continuation_allowed,
                      routing_line, size_multiplier)
from .levels import (CONT_STOP_MAX_PCT, REVERSAL_STOP_MAX_PCT, StructuralLevels,
                     early_failure_exit, levels_allowed, plan_from_bars, stop_ok,
                     structural_stop, trailing_stop)
from .supernova import (PHASES, SupernovaPhase,
                        classify as classify_supernova,
                        classify_bundle as classify_supernova_bundle,
                        telemetry_guard)

__all__ = [
    "Bundle", "StrategyGate", "StrategyResult", "Verdict",
    "blocked", "blocked_by_dilution", "clamp", "failed", "insufficient",
    "passed", "triggered",
    "evaluate_gate", "gate_summary_lines", "run_detectors", "time_of_day_adjustment",
    "no_new_entries", "ALL_PRIMARY_NAMES", "ALL_TRIGGER_NAMES",
    "Cause", "cause_lines", "classify", "extract_cause", "rank_catalysts",
    "CONTINUATION", "REVERSAL", "ArmedState", "Confirmation", "arm", "confirm",
    "mode_for_name", "mode_for_trigger_or_name",
    "TIER_1", "TIER_2", "TIER_3", "catalyst_tier", "continuation_allowed",
    "routing_line", "size_multiplier",
    "CONT_STOP_MAX_PCT", "REVERSAL_STOP_MAX_PCT", "StructuralLevels",
    "early_failure_exit", "levels_allowed", "plan_from_bars", "stop_ok",
    "structural_stop", "trailing_stop",
    "PHASES", "SupernovaPhase", "classify_supernova", "classify_supernova_bundle",
    "telemetry_guard",
]