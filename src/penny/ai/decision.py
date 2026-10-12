"""The single AI decision call.

One alert, one call, one report. The panel this replaces sent the same setup to
several models, scored their agreement, and printed a leaderboard. That cost
several calls per alert to produce a number nobody acted on differently: the
verdict was ENTER or it was not, and the models rarely disagreed about that in
a way that changed the trade.

What is kept is the part that helped: one reader, given one view of the setup,
stating what they would do and why. The methodology lives in
`prompts/methodology_v2.md` and is loaded rather than inlined, so it can be
edited without touching the code and versioned on its own.

Spec 2.0 makes the reader adversarial: the gate verdict is not in the facts, so
the report leads with its own judgment (catalyst quality, strongest risk) rather
than echoing the gate, and every block carries a `SOURCE:` line.

Validation is strict and rejects rather than repairs. A report that claims
ENTER on a gate that did not pass is a contradiction, and quietly fixing it
would hide a real disagreement between the gate and the reader.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "methodology_v2.md"

VERDICTS = ("ENTER", "WAIT", "PASS")
RATINGS = ("STRONG", "MODERATE", "WEAK", "AVOID")
DIRECTIONS = ("rising", "flat", "fading")

# Spec 2.0 B: the four fields, in the order the prompt demands them. VERDICT is
# downstream of the three judgments above it -- a model that cannot name its
# risk and its catalyst quality has nothing to add, and the honest answer is
# CATALYST_QUALITY: unknown / STRONGEST_RISK: none identified, not a confident
# echo of whatever the gate already said.
CATALYST_QUALITIES = ("dilution", "promotional", "real", "mechanical", "unknown")
HONEST_NULL = "none identified"
REQUIRED = ("catalyst_quality", "strongest_risk", "verdict")

# The terms a report uses when it is restating the gate rather than judging the
# setup. One match in STRONGEST_RISK (the field that is supposed to carry the
# model's own judgment) is enough: "the gate score is above the tier threshold"
# adds nothing the reader did not already read in the STRATEGY block.
_ECHO = re.compile(r"\b(tier|gate score|gate_score|detector|strategy gate|blocked_by)\b",
                   re.I)
NULL_RISK = "none identified"


def load_methodology() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - packaging accident
        log.warning("methodology prompt unavailable: %s", exc)
        return "Return one JSON decision report for this long-only setup."


@dataclass
class Decision:
    """A validated decision report, or the reason it was rejected."""

    verdict: str = "PASS"
    rating: str = ""
    confidence: float = 0.0
    # Spec 2.0 B: the adversarial judgment fields.
    catalyst_quality: str = ""
    strongest_risk: str = ""
    invalidation_beyond_rules: str = ""
    # The honest-null case: quality unknown, no risk identified. Accepted, but
    # rendered as "AI: no additional information beyond the gate".
    null: bool = False
    # Spec 2.0 C: provenance. "ai" = a valid additive report; "ai_rejected" =
    # the model answered but its report failed validation or added nothing;
    # "rules" = the call never happened or failed.
    source: str = "rules"
    key_findings: list[str] = field(default_factory=list)
    direction: str = "flat"
    momentum: str = ""
    entries: list[dict] = field(default_factory=list)
    stop: Optional[str] = None
    t1: Optional[str] = None
    t2: Optional[str] = None
    exit_note: str = ""
    verdict_reason: str = ""
    rubrics: list[str] = field(default_factory=list)
    risk_notes: str = ""
    # Feature 5: the confidence the reader sees. `confidence` is what the model
    # claimed; this is that number after the historical clamp, which is the one
    # stored on the tracker and shown in /stats.
    confidence_calibrated: float = 0.0
    clamp_flag: str = ""
    # Feature 6 / 9 / 10: rendered into the AI DECISION block as facts.
    context_lines: list[str] = field(default_factory=list)
    error: str = ""
    model: str = ""
    latency_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "rating": self.rating,
                "confidence": round(self.confidence, 2),
                "direction": self.direction, "model": self.model}


def parse_report(text: str) -> dict:
    """Pull the JSON object out of a model response.

    Models wrap JSON in prose and fences even when told not to. Stripping that
    is a formatting problem, not a correctness one, so it is handled here rather
    than rejected -- unlike a schema violation, which is a correctness problem.
    """
    if not text:
        return {}
    raw = str(text).strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return {}


def validate(data: dict, *, gate_passed: Optional[bool] = None,
             min_additive: bool = True) -> Decision:
    """Check the report against the v2 schema and the long-only rules.

    Returns a Decision whose `.error` is set when the report is rejected. Every
    rejection is a reason the report contradicts the setup, not a formatting
    nit, so they are surfaced rather than patched. Rejections carry
    `source="ai_rejected"`: the model answered, its answer just did not earn a
    place over the rules.
    """
    d = Decision()
    if not isinstance(data, dict) or not data:
        d.error = "no JSON object in the response"
        return d

    missing = [k for k in REQUIRED if not data.get(k)]
    if missing:
        d.error = f"missing required field(s): {', '.join(missing)}"
        return d

    quality = str(data.get("catalyst_quality", "")).strip().lower()
    if quality not in CATALYST_QUALITIES:
        d.error = (f"catalyst_quality must be one of {', '.join(CATALYST_QUALITIES)}")
        return d

    risk = str(data.get("strongest_risk") or "").strip()
    if not risk:
        d.error = "strongest_risk must be a sentence or 'none identified'"
        return d

    verdict = str(data.get("verdict", "")).upper()
    if verdict not in VERDICTS:
        d.error = f"verdict must be one of {', '.join(VERDICTS)}"
        return d

    # Long-only, enforced on the payload rather than trusted from the prompt.
    # Trade instructions only: a risk note that says the move "looks like a
    # fade" is commentary; "sell short here" is a trade and is rejected.
    blob = json.dumps(data).lower()
    if any(w in blob for w in ("short entry", "sell short", "go short", "short below",
                               "short above", "short zone", "going short", "shorting")):
        d.error = "report proposes a short; this is long-only"
        return d

    # The honest null (Spec 2.0 B): no risk the model can name, no judgment on
    # the catalyst. Accepted -- it is informative, and exactly what a pure tape
    # move deserves -- but an ENTER on top of it claims the model has something
    # to add while saying it has nothing.
    d.catalyst_quality = quality
    d.strongest_risk = risk
    d.invalidation_beyond_rules = str(data.get("invalidation_beyond_rules") or "").strip()
    d.null = (risk.lower() == NULL_RISK and quality == "unknown")
    if d.null and verdict == "ENTER":
        d.error = ("verdict=ENTER with STRONGEST_RISK 'none identified' and "
                   "CATALYST_QUALITY unknown: nothing to add, and it did not")
        return d

    # Spec 2.0 B: a report that restates the gate without adding a judgment is
    # non-additive. Acceptance criterion 4.
    if min_additive:
        echo = _ECHO.search(risk)
        if echo:
            d.error = (f"non-additive report: STRONGEST_RISK restates the gate "
                       f"({echo.group(0)}) instead of a setup-specific risk")
            return d

    # Spec 2.0 B: an ENTER can never survive on a symbol the gate blocked. The
    # model is not told the verdict, so this is enforced from what the caller
    # knows, not from an echo in the payload.
    if verdict == "ENTER" and gate_passed is False:
        d.error = "verdict=ENTER but the strategy gate did not pass"
        return d

    # Optional legacy fields, honoured when a model still sends them.
    findings = data.get("key_findings")
    if findings is not None:
        if not isinstance(findings, list) or len(findings) > 4:
            d.error = "key_findings must be a list of at most 4 items"
            return d
        d.key_findings = [str(f) for f in findings][:4]

    rating = data.get("rating")
    if isinstance(rating, dict) and rating:
        grade = str(rating.get("grade", "")).upper()
        if grade and grade not in RATINGS:
            d.error = f"rating.grade must be one of {', '.join(RATINGS)}"
            return d
        d.rating = grade
        try:
            d.confidence = float(rating.get("confidence", 0.0))
        except (TypeError, ValueError):
            d.error = "rating.confidence must be a number"
            return d
        if not 0.0 <= d.confidence <= 1.0:
            d.error = "rating.confidence must be within 0..1"
            return d

    d.verdict = verdict
    d.catalyst_quality = quality
    d.strongest_risk = risk
    d.verdict_reason = str(data.get("verdict_reason", ""))
    d.risk_notes = str(data.get("risk_notes", ""))
    d.key_findings = d.key_findings or [d.strongest_risk]
    # Legacy fields stay optional so a model that keeps the old shape is still
    # readable, but none of them gate the verdict any more: levels belong to
    # the rules plan, not to a model that was never given the gate.
    entries = data.get("entry_points")
    if isinstance(entries, list) and entries:
        d.entries = [e if isinstance(e, dict) else {"zone": str(e)} for e in entries][:2]
    exits = data.get("exit_points") or {}
    if isinstance(exits, dict):
        d.stop = exits.get("stop")
        d.t1 = exits.get("t1")
        d.t2 = exits.get("t2")
        d.exit_note = str(exits.get("note", ""))
    trends = data.get("trends") or {}
    if isinstance(trends, dict):
        d.direction = str(trends.get("direction", "flat"))
        d.momentum = str(trends.get("momentum", ""))
    d.rubrics = [str(r) for r in (data.get("rubrics_applied") or [])]
    d.confidence_calibrated = d.confidence
    return d


def render(d: Decision) -> str:
    """The DECISION block as it appears in the alert.

    Spec 2.0 B: the adversarial fields lead, in the prompt's order, and the
    verdict is downstream of them. Every block ends with its provenance line
    (Spec 2.0 C), so the reader always knows who is talking.
    """
    if not d.ok:
        return ""
    lines = [f"CATALYST QUALITY: {d.catalyst_quality}"]
    if d.null:
        lines.append(f"STRONGEST RISK: {d.strongest_risk}")
        lines.append("AI: no additional information beyond the gate")
    else:
        lines.append(f"STRONGEST RISK: {d.strongest_risk}")
        if d.invalidation_beyond_rules:
            lines.append(f"INVALIDATION BEYOND RULES: {d.invalidation_beyond_rules}")
        shown = d.confidence_calibrated or d.confidence
        head = f"VERDICT: {d.verdict}"
        tail = []
        if d.rating:
            tail.append(d.rating)
        if shown:
            tail.append(f"confidence {shown:.2f}")
        if tail:
            head += f"  ({', '.join(tail)})"
        lines.append(head)
        if d.verdict_reason:
            lines.append(f"REASON: {d.verdict_reason}")
        for f in d.key_findings:
            lines.append(f"- {f}")
        if d.momentum:
            lines.append(f"TREND: {d.direction} - {d.momentum}")
        for i, e in enumerate(d.entries, 1):
            zone = e.get("zone", "?")
            note = e.get("note")
            lines.append(f"ENTRY {i}: {zone}" + (f"  ({note})" if note else ""))
        exits = []
        if d.stop:
            exits.append(f"STOP: {d.stop}")
        if d.t1:
            exits.append(f"T1: {d.t1}")
        if d.t2:
            exits.append(f"T2: {d.t2}")
        if exits:
            lines.append(" | ".join(exits))
        if d.exit_note:
            lines.append(f"EXIT NOTE: {d.exit_note}")
    # The three context lines the spec adds to the AI DECISION block. They are
    # appended after the model's own output because they are facts, not
    # judgements: a sampled analog hit rate and an exit-time estimate are the
    # reader's evidence, and burying them under the AI's prose would make them
    # harder to see rather than easier.
    for line in getattr(d, "context_lines", ()) or ():
        if line:
            lines.append(line)
    # Spec 2.0 C: every decision block ends with a provenance line.
    lines.append("SOURCE: ai")
    return "\n".join(lines)


def build_messages(facts: dict, methodology: Optional[str] = None) -> list[dict]:
    """The single request: methodology as the system turn, facts as the user turn."""
    user = "Setup:\n" + json.dumps(facts, indent=2, default=str)
    context = facts.get("context") or {}
    extra = []
    mtf = context.get("multi_timeframe")
    if isinstance(mtf, dict):
        from ..context.multi_timeframe import block_lines
        extra.append(block_lines(mtf))
    analogs = context.get("analogs")
    if isinstance(analogs, dict) and analogs.get("summary"):
        from ..context.analogs import analog_line
        line = analog_line(analogs["summary"])
        if line:
            extra.append(line)
    sector = context.get("sector")
    if isinstance(sector, str) and sector:
        extra.append(sector)
    liq = context.get("exit_liquidity")
    if isinstance(liq, dict):
        from ..context.liquidity import line as liq_line
        line = liq_line(liq)
        if line:
            extra.append(line)
    prior = context.get("calibration_prior")
    if isinstance(prior, dict):
        from ..context.calibration import prior_lines
        lines = prior_lines(prior, context.get("calibration_label") or "")
        if lines:
            extra.append(lines)
    if extra:
        user = user + "\n\n" + "\n".join(x for x in extra if x)
    return [
        {"role": "system",
         "content": methodology if methodology is not None else load_methodology()},
        {"role": "user", "content": user},
    ]


def gate_ok(facts: dict) -> bool:
    """Did the strategy gate pass for this symbol? Unknown counts as no."""
    gate = facts.get("gate") or {}
    return gate.get("passed") is True


def _apply_calibration(d: Decision, facts: dict) -> None:
    """Clamp a claimed confidence to the bucket's historical hit rate."""
    from ..context.calibration import clamp_confidence
    context = facts.get("context") or {}
    prior = context.get("calibration_prior")
    margin = float(context.get("calibration_margin") or 0.15)
    value, flag = clamp_confidence(d.confidence, prior, margin)
    d.confidence_calibrated = value
    d.clamp_flag = flag
    if flag and flag not in d.risk_notes:
        d.risk_notes = (d.risk_notes + " " if d.risk_notes else "") + flag
    d.context_lines = list(context.get("render_lines") or ())


def decide(gateway, facts: dict, *, model: str = "", deadline: Optional[float] = None,
           max_tokens: int = 700, gate_passed: Optional[bool] = None,
           min_additive: bool = True) -> Decision:
    """Make the one call and validate what comes back.

    Spec 2.0 B: the model is never handed the gate's verdict, so it cannot echo
    it; the ENTER-on-a-blocked-gate contradiction is caught here, server-side,
    from the caller's own knowledge. Rejections are honest about their kind:
    a failed call degrades to the rules (source="rules"), a rejected report
    degrades with source="ai_rejected" and its reason -- never silently to PASS.
    """
    d = Decision(model=model)
    d.source = "rules"
    if gateway is None or not getattr(gateway, "configured", False):
        d.error = "ai: not configured"
        d.verdict = "PASS"
        return d

    result = gateway.chat(model or facts.get("model", ""),
                          build_messages(facts),
                          max_tokens=max_tokens, temperature=0.1,
                          json_mode=True, deadline=deadline)
    d.model = result.model
    d.latency_ms = result.latency_ms
    if not result.ok:
        d.error = result.error or "ai: call failed"
        d.source = "rules"
        return d

    parsed = validate(parse_report(result.text), gate_passed=gate_passed)
    parsed.model = d.model
    parsed.latency_ms = d.latency_ms
    if not parsed.ok:
        parsed.source = "ai_rejected"
        log.info("decision rejected for %s: %s", facts.get("symbol"), parsed.error)
        return parsed
    # An ENTER can never survive on a symbol the gate blocked, regardless of
    # what the model was told: the gate is not in the facts, so this check is
    # server-side on purpose.
    if parsed.verdict == "ENTER" and gate_passed is False:
        parsed.error = "verdict=ENTER but the strategy gate did not pass"
    _apply_calibration(parsed, facts)
    if not parsed.ok:
        parsed.source = "ai_rejected"
        log.info("decision rejected for %s: %s", facts.get("symbol"), parsed.error)
        return parsed
    parsed.source = "ai"
    return parsed


def render_rules_fallback(m, gate, bundle, *, source: str, reason: str,
                          cfg=None) -> str:
    """The rules-only DECISION block (Spec 2.0 C).

    The pipeline always has a view, so an unavailable or rejected AI call never
    leaves a hole in the message. The verdict is the gate's own -- it already
    passed the setup, so saying so is not a new opinion -- and the levels come
    from the plan the rules already computed.
    """
    passed = bool(gate is not None and getattr(gate, "passed", False))
    score = float(getattr(gate, "gate_score", 0.0) or 0.0)
    verdict = "WAIT"
    if passed and (cfg is None or cfg.bool("RULES_FALLBACK_VERDICT", True)):
        verdict = "ENTER"
    rating = "STRONG" if score >= 75 else ("MODERATE" if score >= 55 else "WEAK")

    bits = []
    if passed:
        bits.append("gate pass")
    elif getattr(gate, "blocked_by", None):
        bits.append(f"gate blocked: {gate.blocked_by}")
    reality = next((r for r in (getattr(gate, "strategies", None) or [])
                    if r.name == "spread_liquidity"), None)
    if reality is not None and reality.verdict == "PASS":
        bits.append("reality gate pass")
    mechanism = next((r.name for r in (getattr(gate, "strategies", None) or [])
                      if getattr(r, "verdict", "") == "PASS"
                      and r.name in ("silent_accumulation", "sympathy_move",
                                     "short_squeeze_mechanics")), None)
    label = mechanism or _cause_label(bundle)
    if label:
        bits.append(f"mechanism: {label}" if mechanism else f"cause: {label}")

    plan = getattr(bundle, "plan", None) if bundle is not None else None
    if plan is not None and getattr(plan, "stop", None):
        levels = f"stop {plan.stop:.2f}"
        if getattr(plan, "target1", None):
            levels += f" | T1 {plan.target1:.2f}"
        if getattr(plan, "target2", None):
            levels += f" | T2 {plan.target2:.2f}"
        levels = f"LEVELS: {levels}"
    else:
        levels = "LEVELS: none computed"

    lines = [
        "\u2500\u2500 DECISION (rules-only) \u2500",
        f"VERDICT: {verdict}",
        f"RATING: {rating}",
        "BASIS: " + (" + ".join(bits) if bits else "evidence incomplete"),
        levels,
        f"SOURCE: {source}",
        "NOTE: no adversarial check \u2014 treat as unverified",
    ]
    if source == "ai_rejected" and reason:
        lines.append(f"REASON: {reason}")
    return "\n".join(lines)


def _cause_label(bundle) -> Optional[str]:
    """A short cause label for the rules-only BASIS line, or None."""
    if bundle is None or getattr(bundle, "news", None) is None:
        return None
    try:
        from ..strategies.news_cause import rank_catalysts, extract_cause
        _, ranked, _ = rank_catalysts(bundle)
        cause = extract_cause(ranked, bundle)
        if cause and cause.kind != "UNEXPLAINED":
            return f"cause: {cause.text[:60]}"
    except Exception:  # noqa: BLE001
        return None
    return None
