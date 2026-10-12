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

__all__ = [
    "Bundle", "StrategyGate", "StrategyResult", "Verdict",
    "blocked", "blocked_by_dilution", "clamp", "failed", "insufficient",
    "passed", "triggered",
    "evaluate_gate", "gate_summary_lines", "run_detectors", "time_of_day_adjustment",
    "no_new_entries", "ALL_PRIMARY_NAMES", "ALL_TRIGGER_NAMES",
    "Cause", "cause_lines", "classify", "extract_cause", "rank_catalysts",
]