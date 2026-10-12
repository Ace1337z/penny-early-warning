"""The single AI decision call.

One alert, one call, one report. The panel this replaces sent the same setup to
several models, scored their agreement, and printed a leaderboard. That cost
several calls per alert to produce a number nobody acted on differently: the
verdict was ENTER or it was not, and the models rarely disagreed about that in
a way that changed the trade.

What is kept is the part that helped: one reader, given one view of the setup,
stating what they would do and why. The methodology lives in
`prompts/methodology_v1.md` and is loaded rather than inlined, so it can be
edited without touching the code and versioned on its own.

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

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "methodology_v1.md"

VERDICTS = ("ENTER", "WAIT", "PASS")
RATINGS = ("STRONG", "MODERATE", "WEAK", "AVOID")
DIRECTIONS = ("rising", "flat", "fading")

REQUIRED = ("key_findings", "rating", "trends", "entry_points", "exit_points", "verdict")


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
    rating: str = "WEAK"
    confidence: float = 0.0
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


def validate(data: dict, *, gate_passed: Optional[bool] = None) -> Decision:
    """Check the report against the schema and the long-only rules.

    Returns a Decision whose `.error` is set when the report is rejected. Every
    rejection is a reason the report contradicts the setup, not a formatting
    nit, so they are surfaced rather than patched.
    """
    d = Decision()
    if not isinstance(data, dict) or not data:
        d.error = "no JSON object in the response"
        return d

    missing = [k for k in REQUIRED if not data.get(k)]
    if missing:
        d.error = f"missing required field(s): {', '.join(missing)}"
        return d

    findings = data.get("key_findings")
    if not isinstance(findings, list) or len(findings) > 4:
        d.error = "key_findings must be a list of at most 4 items"
        return d

    rating = data.get("rating") or {}
    grade = str(rating.get("grade", "")).upper()
    if grade not in RATINGS:
        d.error = f"rating.grade must be one of {', '.join(RATINGS)}"
        return d
    try:
        confidence = float(rating.get("confidence", 0.0))
    except (TypeError, ValueError):
        d.error = "rating.confidence must be a number"
        return d
    if not 0.0 <= confidence <= 1.0:
        d.error = "rating.confidence must be within 0..1"
        return d

    trends = data.get("trends") or {}
    direction = str(trends.get("direction", "")).lower()
    if direction not in DIRECTIONS:
        d.error = f"trends.direction must be one of {', '.join(DIRECTIONS)}"
        return d

    entries = data.get("entry_points")
    if not isinstance(entries, list) or not entries:
        d.error = "entry_points must list at least one LONG zone"
        return d
    if len(entries) > 2:
        entries = entries[:2]
    # Long-only, enforced on the payload rather than trusted from the prompt.
    for e in entries:
        blob = json.dumps(e).lower()
        if any(w in blob for w in ("short entry", "sell short", "go short", "short below",
                                   "short above", "short zone", "fade", "going short")):
            d.error = "entry_points contains a short or fade zone; this is long-only"
            return d

    exits = data.get("exit_points") or {}
    stop = exits.get("stop")
    if not stop:
        d.error = "exit_points.stop is required: an entry without a stop is not a plan"
        return d

    verdict = str(data.get("verdict", "")).upper()
    if verdict not in VERDICTS:
        d.error = f"verdict must be one of {', '.join(VERDICTS)}"
        return d

    rubrics = data.get("rubrics_applied") or []
    if not isinstance(rubrics, list):
        rubrics = []
    if verdict == "ENTER" and not rubrics:
        d.error = "verdict=ENTER requires at least one rubric_applied"
        return d

    reported_gate = (data.get("gate") or {}).get("passed")
    effective_gate = reported_gate if reported_gate is not None else gate_passed
    if verdict == "ENTER" and effective_gate is False:
        d.error = "verdict=ENTER but the strategy gate did not pass"
        return d

    if verdict == "ENTER" and not entries:
        d.error = "verdict=ENTER requires an entry zone"
        return d

    d.verdict = verdict
    d.rating = grade
    d.confidence = confidence
    d.key_findings = [str(f) for f in findings][:4]
    d.direction = direction
    d.momentum = str(trends.get("momentum", ""))
    d.entries = [e if isinstance(e, dict) else {"zone": str(e)} for e in entries]
    d.stop = stop
    d.t1 = exits.get("t1")
    d.t2 = exits.get("t2")
    d.exit_note = str(exits.get("note", ""))
    d.verdict_reason = str(data.get("verdict_reason", ""))
    d.rubrics = [str(r) for r in rubrics]
    d.risk_notes = str(data.get("risk_notes", ""))
    return d


def render(d: Decision) -> str:
    """The DECISION block as it appears in the alert.

    Verdict first, because that is what the reader is looking for. Findings next,
    because they are the evidence for it. The rest is reference.
    """
    if not d.ok:
        return ""
    lines = [
        f"VERDICT: {d.verdict}  ({d.rating}, confidence {d.confidence:.2f})",
    ]
    if d.verdict_reason:
        lines.append(f"REASON: {d.verdict_reason}")
    for f in d.key_findings:
        lines.append(f"- {f}")
    lines.append(f"TREND: {d.direction} - {d.momentum}" if d.momentum
                 else f"TREND: {d.direction}")
    for i, e in enumerate(d.entries, 1):
        zone = e.get("zone", "?")
        note = e.get("note")
        lines.append(f"ENTRY {i}: {zone}" + (f"  ({note})" if note else ""))
    exits = [f"STOP: {d.stop}"]
    if d.t1:
        exits.append(f"T1: {d.t1}")
    if d.t2:
        exits.append(f"T2: {d.t2}")
    lines.append(" | ".join(exits))
    if d.exit_note:
        lines.append(f"EXIT NOTE: {d.exit_note}")
    if d.rubrics:
        lines.append(f"APPLIED: {', '.join(d.rubrics[:3])}")
    if d.risk_notes:
        lines.append(f"RISK: {d.risk_notes}")
    return "\n".join(lines)


def build_messages(facts: dict, methodology: Optional[str] = None) -> list[dict]:
    """The single request: methodology as the system turn, facts as the user turn."""
    return [
        {"role": "system",
         "content": methodology if methodology is not None else load_methodology()},
        {"role": "user",
         "content": "Setup:\n" + json.dumps(facts, indent=2, default=str)},
    ]


def gate_ok(facts: dict) -> bool:
    """Did the strategy gate pass for this symbol? Unknown counts as no."""
    gate = facts.get("gate") or {}
    return gate.get("passed") is True


def decide(gateway, facts: dict, *, model: str = "", deadline: Optional[float] = None,
           max_tokens: int = 700) -> Decision:
    """Make the one call and validate what comes back.

    A rejected report degrades to PASS rather than to ENTER. That is the safe
    direction: the setup may well be good, but a report that contradicts its own
    gate has not earned an entry recommendation.
    """
    d = Decision(model=model)
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
        return d

    parsed = validate(parse_report(result.text),
                      gate_passed=(facts.get("gate") or {}).get("passed"))
    # A model that does not echo the gate verdict gets the real one applied to it,
    # so an ENTER can never survive on a symbol the gate blocked.
    if parsed.ok and parsed.verdict == "ENTER" and not gate_ok(facts):
        parsed.error = "verdict=ENTER but the strategy gate did not pass"
    parsed.model = d.model
    parsed.latency_ms = d.latency_ms
    if not parsed.ok:
        parsed.verdict = "PASS"
        log.info("decision rejected for %s: %s", facts.get("symbol"), parsed.error)
    return parsed
