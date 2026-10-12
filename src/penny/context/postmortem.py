"""A post-mortem on every losing alert: which clause should have blocked.

The valuable question about a loss is not "was it a loss" -- it is whether the
system's own rules should have said no and did not. A winner teaches nothing
new; a loss that a specific rubric clause would have caught teaches exactly one
thing, and only if the clause is named.

The mapping below is deliberately small and concrete. Each entry pairs a clause
with the observation that should have triggered it, so the suggestion at the end
is a threshold or classifier change rather than a platitude. Where no clause
matches, the post-mortem says so: "nothing in the rubric covered this" is a
useful finding, and a fabricated clause number would be worse than silence.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


def is_loser(row: dict) -> bool:
    """A losing alert: closed red, stopped out, or invalidated."""
    if row.get("status") == "aborted":
        return True
    if row.get("hit_stop"):
        return True
    close = row.get("close_pct")
    return close is not None and float(close) < 0


def _find_clause(row: dict) -> tuple:
    """(clause, what should have blocked, the change to make)."""
    primary = str(row.get("primary_strategy") or "").lower()
    abort = str(row.get("abort_reason") or "").lower()

    if "halt" in abort:
        return ("R3 halt clause",
                "it halted shortly after the alert and was not detectable at the gate",
                "add a halt pre-check to spread_liquidity")
    if "spread" in abort:
        return ("R2 spread clause",
                "the spread blew out after the entry",
                "tighten the post-entry spread ceiling")
    if "offering" in abort or "dilut" in abort or "reverse split" in abort:
        return ("R4 negative-catalyst clause",
                "a dilutive filing landed after the alert",
                "re-check catalysts on any new 8-K while a setup is tracked")

    close = row.get("close_pct")
    peak = row.get("max_gain_pct")
    if peak is not None and close is not None and float(peak) < 0.02 and float(close) < 0:
        return ("R1 momentum clause",
                "it never ran -- peak gain under 2% and it closed red",
                "require a minimum first-leg extension before alerting on %s"
                % (primary or "this pattern"))
    if close is not None and float(close) < -0.10:
        return ("R1 momentum clause",
                "it gave back more than 10% from the alert price",
                "abort hard on a 10% drawdown from the alert")
    if primary == "low_float_squeeze" and row.get("analog_stop_hit_rate") is None:
        return ("R5 float clause",
                "low-float squeezes stopped out more often than they ran",
                "require a confirmed catalyst class before a squeeze alert")

    return ("", "no existing rubric clause matches this loss", "manual review")


def build(row: dict) -> Optional[str]:
    """The one-paragraph post-mortem for a losing alert, or None if it won."""
    if not is_loser(row):
        return None
    sym = row.get("symbol", "?")
    primary = row.get("primary_strategy") or "unknown"
    score = row.get("gate_score")
    rating = row.get("ai_rating") or "unrated"
    verdict = row.get("ai_verdict") or "?"
    findings = str(row.get("ai_key_findings") or "")[:200]
    clause, went_wrong, suggestion = _find_clause(row)

    head = "%s entered on %s" % (sym, primary)
    if score is not None:
        head += " (gate score %.0f)" % float(score)
    parts = [head + "; the AI rated it %s / %s." % (rating, verdict)]
    if findings:
        parts.append("Its findings: %s." % findings)
    if clause:
        parts.append("%s should have blocked -- %s." % (clause, went_wrong))
    else:
        parts.append("Nothing in the rubric covered this: %s." % went_wrong)
    parts.append("Suggest: %s." % suggestion)
    return " ".join(" ".join(parts).split())


def short_line(row: dict) -> str:
    """The one-line version for the EOD POST-MORTEMS section."""
    sym = row.get("symbol", "?")
    primary = row.get("primary_strategy") or "unknown"
    clause, _, suggestion = _find_clause(row)
    if clause:
        tail = "%s should have blocked. Suggest: %s." % (clause, suggestion)
    else:
        tail = "no clause covered this. Suggest: %s." % suggestion
    return "%s  %s; %s" % (sym, primary, tail)
