"""The one AI call: the v2 adversarial schema, long-only rules, strict rejection.

Spec 2.0 changes what the model is asked. It no longer returns a verdict as its
headline -- it returns CATALYST_QUALITY, STRONGEST_RISK, INVALIDATION_BEYOND_RULES
and only then VERDICT -- and it is never handed the gate's verdict, so agreement
carries no information and echoing it is impossible. Rejection stays strict: a
report that contradicts its own setup, or merely restates the gate, is refused,
and a refusal degrades to the rules-only block, never silently to PASS.
"""

from __future__ import annotations

import json
import time

from penny.ai.decision import (VERDICTS, Decision, build_messages, decide,
                               load_methodology, parse_report, render, validate)
from penny.ai.gateway import FakeGateway


def _report(**over):
    base = {
        "catalyst_quality": "real",
        "strongest_risk": ("average trade size of $180 on 9x volume is retail "
                           "churn, not accumulation"),
        "invalidation_beyond_rules": "two 1m closes under the 10:40 trendline",
        "key_findings": ["reclaimed VWAP on 9x volume", "float is 18M"],
        "rating": {"grade": "MODERATE", "confidence": 0.62},
        "trends": {"direction": "rising", "momentum": "3m accel"},
        "entry_points": [{"zone": "1.40-1.42", "note": "on a hold of VWAP"}],
        "exit_points": {"stop": "1.33", "overhead": ["1.55", "1.70"]},
        "verdict": "ENTER",
    }
    base.update(over)
    return base


def _facts(**over):
    base = {"symbol": "TEST", "price": 1.41, "session": "regular", "news": [],
            "market": "MARKET SPY +0.4% | RISK-ON",
            "cause": ["CAUSE: Acme Corp awarded $12M contract (Benzinga, 2m)"],
            "cause_detail": {"headline": "Acme Corp awarded $12M contract",
                             "outlet": "Benzinga", "age_minutes": 2.0,
                             "class": "HARD", "url": "http://x/y"}}
    base.update(over)
    return base


# --- the happy path ----------------------------------------------------------

def test_a_complete_report_validates():
    d = validate(_report())
    assert d.ok, d.error
    assert d.verdict == "ENTER"
    assert d.catalyst_quality == "real"
    assert d.strongest_risk.startswith("average trade size")
    assert d.rating == "MODERATE"
    assert d.confidence == 0.62
    assert d.stop == "1.33" and d.overhead == ["1.55", "1.70"]


def test_render_leads_with_the_four_fields_in_order_and_ends_with_source():
    text = render(validate(_report()))
    lines = text.splitlines()
    assert lines[0] == "CATALYST QUALITY: real"
    assert lines[1].startswith("STRONGEST RISK:")
    assert lines[2] == "INVALIDATION BEYOND RULES: 1.40-1.42" or \
        lines[2].startswith("INVALIDATION BEYOND RULES:")
    assert any(l.startswith("VERDICT: ENTER") for l in lines)
    # Spec 2.0 C: every decision block ends with a provenance line.
    assert lines[-1] == "SOURCE: ai"
    assert "ENTRY 1: 1.40-1.42" in text
    assert "STOP (structural): 1.33" in text and "OVERHEAD: 1.55, 1.70" in text
    # Spec 4.2: a ghost target must never appear in a rendered decision.
    assert "T1:" not in text and "T2:" not in text


def test_an_honest_null_is_accepted_and_marked():
    """Criterion 5: none identified + unknown is accepted, and marked."""
    d = validate(_report(catalyst_quality="unknown",
                         strongest_risk="none identified", verdict="NO_TRADE",
                         invalidation_beyond_rules=""))
    assert d.ok, d.error
    assert d.null
    text = render(d)
    assert "AI: no additional information beyond the gate" in text


def test_an_honest_null_with_enter_is_rejected():
    d = validate(_report(catalyst_quality="unknown",
                         strongest_risk="none identified"))
    assert not d.ok
    assert "nothing to add" in d.error


def test_a_rejected_report_renders_as_nothing():
    """A rejected decision must not print half a decision."""
    assert render(Decision(error="rejected")) == ""


# --- the schema is required; missing keys are rejected, not repaired ---------

def test_each_required_field_is_actually_required():
    for field in ("catalyst_quality", "strongest_risk", "verdict"):
        broken = _report()
        broken.pop(field)
        d = validate(broken)
        assert not d.ok, f"{field} should be required"
        assert field in d.error


def test_an_empty_response_is_rejected():
    assert not validate({}).ok
    assert not validate("not a dict").ok


def test_an_unknown_catalyst_quality_is_rejected():
    assert not validate(_report(catalyst_quality="spicy")).ok


def test_legacy_optional_fields_stay_readable():
    """A model that still sends the v1 shape is read, not rejected."""
    d = validate(_report(trends={"direction": "sideways"}))
    assert d.ok  # trends are optional now, not a gate on the verdict


def test_a_confidence_outside_zero_to_one_is_rejected():
    assert not validate(_report(rating={"grade": "WEAK", "confidence": 1.4})).ok
    assert not validate(_report(rating={"grade": "WEAK", "confidence": -0.1})).ok


# --- non-additive: a report that restates the gate earns nothing -------------

def test_a_report_that_restates_the_gate_is_rejected_as_non_additive():
    """Criterion 4."""
    d = validate(_report(strongest_risk="the gate score is above the tier threshold"))
    assert not d.ok
    assert "non-additive" in d.error


def test_a_specific_risk_survives_the_additive_check():
    assert validate(_report()).ok


def test_an_honest_null_is_never_a_rejection():
    d = validate(_report(catalyst_quality="unknown",
                         strongest_risk="none identified", verdict="NO_TRADE"))
    assert d.ok and d.null


# --- long-only, enforced on the payload rather than trusted from the prompt --

def test_every_verdict_is_a_long_verdict():
    # Spec 5: WAIT is banned; the four outcomes are ENTER/ARMED/NO_TRADE/REJECT.
    assert set(VERDICTS) == {"ENTER", "ARMED", "NO_TRADE", "REJECT"}
    assert "WAIT" not in VERDICTS
    assert not validate(_report(verdict="WAIT")).ok
    assert not validate(_report(verdict="SHORT")).ok
    assert not validate(_report(verdict="SELL")).ok


def test_a_short_instruction_anywhere_in_the_report_is_rejected():
    assert not validate(_report(strongest_risk="the move dies: sell short here")).ok
    assert not validate(_report(invalidation_beyond_rules="going short under VWAP")).ok


def test_a_risk_note_mentioning_short_float_is_not_a_trade():
    """Commentary is not a trade instruction; only instructions are rejected."""
    assert validate(_report(strongest_risk="short float 22% is the fuel")).ok


def test_an_enter_on_a_gate_that_did_not_pass_is_rejected():
    """The contradiction that must never be silently repaired (server-side)."""
    d = validate(_report(), gate_passed=False)
    assert not d.ok
    assert "gate" in d.error


# --- parsing ----------------------------------------------------------------

def test_a_fenced_json_block_parses():
    raw = "```json\n" + json.dumps(_report()) + "\n```"
    assert validate(parse_report(raw)).ok


def test_prose_around_the_json_does_not_break_parsing():
    raw = "Here is my read:\n" + json.dumps(_report()) + "\nThat is the plan."
    assert validate(parse_report(raw)).ok


def test_html_in_place_of_json_is_rejected():
    d = validate(parse_report("<html><body>not json</body></html>"))
    assert not d.ok


def test_an_empty_completion_is_rejected():
    assert not validate(parse_report("")).ok


# --- the prompt --------------------------------------------------------------

def test_the_methodology_is_a_real_file_on_disk():
    method = load_methodology()
    assert len(method) > 200
    assert "CATALYST_QUALITY" in method
    assert "STRONGEST_RISK" in method
    assert "VERDICT" in method


def test_the_methodology_states_long_only_and_forbids_roleplay():
    method = load_methodology().lower()
    assert "long only" in method
    assert "roleplay" in method


def test_the_request_carries_the_methodology_and_the_facts():
    messages = build_messages(_facts())
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "catalyst_quality" in messages[0]["content"]
    body = json.loads(messages[1]["content"].split("Setup:\n", 1)[1])
    assert body["symbol"] == "TEST"
    assert body["cause"][0].startswith("CAUSE:")
    assert body["cause_detail"]["outlet"]


# --- the fact bundle must not leak the gate (criterion 7) ---------------------

def test_the_fact_bundle_never_carries_the_tier_score_or_gate_verdict():
    from penny.alerts import build_ai_facts
    from penny.config import Config
    from penny.scoring import Metrics, Scorer

    cfg = Config()
    scorer = Scorer(cfg)
    scorer.set_avg_volume("TEST", 400_000.0)
    m = None
    for i in range(12):
        out = scorer.compute({
            "symbol": "TEST", "ts": time.time() + i * 60, "price": 1.41,
            "cum_volume": 900_000 + i * 60_000, "prev_close": 1.00,
            "high_price": 1.45,
        }, "regular")
        if out is not None:
            m = out
    assert m is not None
    facts = build_ai_facts(m, None, None, session="regular")
    blob = json.dumps(facts).lower()
    for banned in ("gate", "gate_score", "tier", "blocked_by", "passed"):
        assert f'"{banned}"' not in blob, banned


# --- the call itself ---------------------------------------------------------


# --- the call itself ---------------------------------------------------------

def test_one_alert_makes_exactly_one_call():
    gateway = FakeGateway(models=["m"])
    d = decide(gateway, _facts(), model="m", gate_passed=True)
    assert d.ok, d.error
    assert len(gateway.calls) == 1


def test_a_gate_passer_comes_back_as_enter():
    d = decide(FakeGateway(models=["m"]), _facts(), model="m", gate_passed=True)
    assert d.verdict == "ENTER"


def test_a_gate_blocked_symbol_can_never_come_back_as_enter():
    """The model is not told the verdict, so this check is server-side."""
    d = decide(FakeGateway(models=["m"]), _facts(), model="m", gate_passed=False)
    assert not d.ok
    assert d.source == "ai_rejected"
    assert "gate" in d.error


def test_an_unconfigured_gateway_yields_a_rules_source_not_a_crash():
    class _Off:
        configured = False

    d = decide(_Off(), _facts(), model="m")
    assert not d.ok
    assert d.source == "rules"


def test_a_failing_model_yields_a_rules_source_not_a_crash():
    gateway = FakeGateway(models=["m"])
    gateway.fail("m")
    d = decide(gateway, _facts(), model="m")
    assert not d.ok
    assert d.source == "rules"


def test_a_non_json_answer_is_rejected_rather_than_guessed_at():
    gateway = FakeGateway(models=["m"])
    gateway.make_invalid("m")
    d = decide(gateway, _facts(), model="m")
    assert not d.ok
    assert d.source == "ai_rejected"
