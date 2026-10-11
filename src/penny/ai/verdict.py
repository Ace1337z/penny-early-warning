"""Portfolio-manager verdict: ENTER / WAIT / PASS on top of the panel (6.11).

The panel reports what the models think will happen. This module decides whether
that is *actionable now*, and it only ever tightens the panel's own answer -- it
cannot turn a PASS into an ENTER, because a model that says "no" with a reason is
more information than the rules in this file.

Three ideas do the work:

- **Directional bias is not tradeability.** A stock can be unambiguously going up
  and still be unbuyable: it is already at the expected peak, it has run 12% in
  five minutes, or the R:R on the plan is 0.8:1. Those are WAIT, not ENTER.
- **Hard gates before soft ones.** A data-quality failure and a plan below the
  minimum R:R are decided from the facts alone, so they survive a panel that
  answered confidently and wrongly. Everything else is soft.
- **No invented numbers.** Every reason string quotes a number the caller passed
  in. Nothing here computes a price the facts did not already contain.
"""

from __future__ import annotations

from .panel import AggregateForecast

VERDICT_ENTER = "ENTER"
VERDICT_WAIT = "WAIT"
VERDICT_PASS = "PASS"

# Ranked so the most cautious of several verdicts wins.
_RANK = {VERDICT_PASS: 0, VERDICT_WAIT: 1, VERDICT_ENTER: 2}


def _tightest(*verdicts: str) -> str:
    """The most cautious of several verdicts; an unknown input is ignored."""
    known = [v for v in verdicts if v in _RANK]
    return min(known, key=lambda v: _RANK[v]) if known else VERDICT_WAIT


def data_conflict(facts: dict) -> str:
    """Return a reason when the facts cannot be trusted, else "".

    A stock whose quote and its verified price disagree has no analysable setup,
    and the only correct output is PASS / DATA_CONFLICT. The cases seen in
    practice are a verification mismatch and a missing price.
    """
    price = facts.get("price")
    if not isinstance(price, (int, float)) or price <= 0:
        return "no usable price in the facts"

    verification = facts.get("finviz") or {}
    vprice = verification.get("price")
    if (isinstance(vprice, (int, float)) and vprice > 0
            and abs(price - vprice) / vprice > 0.05):
        return ("quote %g disagrees with the verified price %g by more than 5%%"
                % (price, vprice))
    if verification.get("authoritative") is False:
        return "the verification source marked this row as not authoritative"
    return ""


def plan_conflict(facts: dict, min_rr: float) -> str:
    """Return a reason when a supplied plan must not be acted on, else ""."""
    plan = facts.get("trade_plan") or {}
    if not plan:
        return ""
    if plan.get("viable", True):
        return ""
    rr = plan.get("reward_risk")
    shown = ("%.2f:1" % rr) if isinstance(rr, (int, float)) else "n/a"
    return "plan reward:risk %s is below the %g:1 minimum" % (shown, min_rr)


def _soft_verdict(agg: AggregateForecast, facts: dict) -> tuple[str, str]:
    """Blend the signals that can only push the panel's call down, never up."""
    price = facts.get("price")
    extents: list[tuple[float, str]] = []
    if isinstance(price, (int, float)) and price > 0:
        peak = agg.expected_peak
        if isinstance(peak, (int, float)) and 0 < peak <= price * 1.01:
            extents.append((price / peak - 1.0,
                            "price %g is already at or past the expected peak %g"
                            % (price, peak)))
        volx = facts.get("volx15")
        if isinstance(volx, (int, float)) and volx < 1.0:
            extents.append((volx, "volume is only %.2fx the 15-minute average -- "
                                  "nothing behind the move" % volx))
        fh = facts.get("from_high")
        if isinstance(fh, (int, float)) and fh <= -15.0:
            # A positive extent is a reason NOT to enter. A stock deep under its
            # high has already given the move back; it is not one with room left.
            extents.append((abs(fh) / 100.0, "price is %.0f%% off the day's high, "
                                         "already giving the move back" % abs(fh)))

    # Each extent is the fraction of the expected move already spent, or missing.
    # The blend is an expected-value estimate, not a probability.
    score = min((e for e, _ in extents), default=0.0)
    if agg.direction != "up":
        return _tightest(agg.verdict, VERDICT_PASS), "panel direction is %s" % agg.direction
    if agg.durability == "fade-prone":
        return _tightest(agg.verdict, VERDICT_WAIT), "panel reads the move as fade-prone"
    if not extents:
        # Nothing argues against entering: that is the panel's call to make.
        return agg.verdict or VERDICT_ENTER, ""
    if score > 0.0:
        return _tightest(agg.verdict, VERDICT_WAIT), max(extents, key=lambda e: e[0])[1]
    if score <= -0.10:
        return (agg.verdict or VERDICT_ENTER,
                "about %.0f%% of the move is left" % (abs(score) * 100))
    return agg.verdict or VERDICT_ENTER, ""


def decide(agg: AggregateForecast, facts: dict, *, min_rr: float = 2.0) -> AggregateForecast:
    """Set `agg.verdict` and `agg.verdict_reason` in place, then return agg.

    Precedence: data-quality failure, then the reward:risk gate, then the panel's
    own call. `facts` is the same object sent to the models, so every number in a
    reason string came from a measured value.
    """
    conflict = data_conflict(facts)
    if conflict:
        agg.data_conflict = conflict
        agg.verdict = VERDICT_PASS
        agg.verdict_reason = "DATA_CONFLICT: " + conflict
        return agg

    plan = plan_conflict(facts, min_rr)
    if plan:
        agg.verdict = VERDICT_PASS
        agg.verdict_reason = plan
        return agg

    if agg.n_models == 0:
        agg.verdict = VERDICT_WAIT
        agg.verdict_reason = "no model answered; nothing to act on"
        return agg

    agg.verdict, agg.verdict_reason = _soft_verdict(agg, facts)
    return agg


def verdict_text(agg: AggregateForecast) -> str:
    """The concise PM line: verdict plus the one reason it is that."""
    line = "PM: " + (agg.verdict or VERDICT_WAIT)
    if agg.verdict_reason:
        line += " | " + agg.verdict_reason
    return line


def why_text(agg: AggregateForecast, max_sentences: int = 3) -> str:
    """The WHY block: a few sentences at most, and no number the facts lack."""
    sentences = []
    if agg.why_now:
        sentences.append(str(agg.why_now).strip())
    elif agg.catalyst:
        sentences.append(str(agg.catalyst).strip())
    if agg.reasons:
        sentences.append("; ".join(str(r) for r in agg.reasons[:2]))
    out = " ".join(s for s in sentences if s)
    if not out:
        return ""
    parts = [p.strip() for p in out.replace("!", ".").replace("?", ".").split(".")
             if p.strip()]
    return ". ".join(parts[:max_sentences])
