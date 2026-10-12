"""The one AI call: schema, long-only rules, and strict rejection.

The panel this replaces repaired bad model output and scored agreement. Strict
rejection is the point here: a report that says ENTER on a gate that did not
pass is a contradiction, and quietly fixing it hides a real disagreement.
"""

from __future__ import annotations

import json

from penny.ai.decision import (VERDICTS, Decision, build_messages, decide,
                               load_methodology, parse_report, render, validate)
from penny.ai.gateway import FakeGateway


def _report(**over):
    base = {
        "key_findings": ["reclaimed VWAP on 9x volume", "float is 18M"],
        "rating": {"grade": "MODERATE", "confidence": 0.62},
        "trends": {"direction": "rising", "momentum": "3m accel"},
        "entry_points": [{"zone": "1.40-1.42", "note": "on a hold of VWAP"}],
        "exit_points": {"stop": "1.33", "t1": "1.55", "t2": "1.70"},
        "verdict": "ENTER",
        "rubrics_applied": ["catalyst + volume", "float < 20M"],
        "gate": {"passed": True},
    }
    base.update(over)
    return base


def _facts(**over):
    base = {"symbol": "TEST", "price": 1.41, "session": "regular", "news": [],
            "market": "MARKET SPY +0.4% | RISK-ON",
            "gate": {"passed": True, "blocked_by": "", "gate_score": 71.0,
                     "primary": ["low_float_squeeze"], "entry_trigger": None},
            "cause": ["CAUSE: Acme Corp awarded $12M contract (Benzinga, 2m)"]}
    base.update(over)
    return base


# --- the happy path ----------------------------------------------------------

def test_a_complete_report_validates():
    d = validate(_report())
    assert d.ok, d.error
    assert d.verdict == "ENTER"
    assert d.rating == "MODERATE"
    assert d.confidence == 0.62
    assert d.direction == "rising"
    assert d.stop == "1.33" and d.t1 == "1.55" and d.t2 == "1.70"
    assert len(d.entries) == 1


def test_render_leads_with_the_verdict_and_carries_the_levels():
    text = render(validate(_report()))
    assert text.splitlines()[0].startswith("VERDICT: ENTER")
    assert "ENTRY 1: 1.40-1.42" in text
    assert "STOP: 1.33" in text and "T1: 1.55" in text
    assert "APPLIED: catalyst + volume" in text


def test_a_rejected_report_renders_as_nothing():
    """A rejected decision must not print half a decision."""
    assert render(Decision(error="rejected")) == ""


# --- the schema is required; missing keys are rejected, not repaired ---------

def test_each_required_field_is_actually_required():
    for field in ("key_findings", "rating", "trends", "entry_points",
                  "exit_points", "verdict"):
        broken = _report()
        broken.pop(field)
        d = validate(broken)
        assert not d.ok, f"{field} should be required"
        assert field in d.error


def test_an_empty_response_is_rejected():
    assert not validate({}).ok
    assert not validate("not a dict").ok


def test_a_missing_stop_is_rejected_because_an_entry_needs_an_exit():
    d = validate(_report(exit_points={"t1": "1.55"}))
    assert not d.ok
    assert "stop" in d.error


def test_an_unknown_grade_or_direction_is_rejected():
    assert not validate(_report(rating={"grade": "MOON", "confidence": 0.5})).ok
    assert not validate(_report(trends={"direction": "sideways"})).ok


def test_a_confidence_outside_zero_to_one_is_rejected():
    assert not validate(_report(rating={"grade": "WEAK", "confidence": 1.4})).ok
    assert not validate(_report(rating={"grade": "WEAK", "confidence": -0.1})).ok


# --- long-only, enforced on the payload rather than trusted from the prompt --

def test_every_verdict_is_a_long_verdict():
    assert set(VERDICTS) == {"ENTER", "WAIT", "PASS"}
    assert not validate(_report(verdict="SHORT")).ok
    assert not validate(_report(verdict="SELL")).ok


def test_a_short_entry_zone_is_rejected():
    for zone in ({"zone": "short below 1.30"}, {"zone": "1.30 sell short"},
                 {"zone": "go short 1.30"}, {"zone": "fade to 1.30"}):
        assert not validate(_report(entry_points=[zone])).ok, zone


def test_a_long_zone_survives_the_short_check():
    assert validate(_report(entry_points=[{"zone": "1.40-1.42"}])).ok


def test_an_enter_on_a_gate_that_did_not_pass_is_rejected():
    """The contradiction that must never be silently repaired."""
    d = validate(_report(gate={"passed": False}))
    assert not d.ok
    assert "gate" in d.error


def test_an_enter_with_no_rubric_is_rejected():
    d = validate(_report(rubrics_applied=[]))
    assert not d.ok
    assert "rubric" in d.error


def test_a_wait_never_needs_a_rubric():
    assert validate(_report(verdict="WAIT", rubrics_applied=[])).ok


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
    assert "key_findings" in method


def test_the_methodology_states_long_only_and_forbids_roleplay():
    method = load_methodology().lower()
    assert "long-only" in method
    assert "roleplay" in method


def test_the_request_carries_the_methodology_and_the_facts():
    messages = build_messages(_facts())
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "key_findings" in messages[0]["content"]
    body = json.loads(messages[1]["content"].split("Setup:\n", 1)[1])
    assert body["symbol"] == "TEST"
    # The gate verdict and the one named cause reach the model, not a headline dump.
    assert body["gate"]["passed"] is True
    assert body["cause"][0].startswith("CAUSE:")


# --- the call itself ---------------------------------------------------------

def test_one_alert_makes_exactly_one_call():
    gateway = FakeGateway(models=["m"])
    d = decide(gateway, _facts(), model="m")
    assert d.ok, d.error
    assert len(gateway.calls) == 1


def test_a_gate_passer_comes_back_as_enter():
    d = decide(FakeGateway(models=["m"]), _facts(), model="m")
    assert d.verdict == "ENTER"


def test_a_gate_blocked_symbol_can_never_come_back_as_enter():
    facts = _facts(gate={"passed": False, "blocked_by": "DILUTION", "gate_score": 0.0,
                         "primary": None, "entry_trigger": None})
    d = decide(FakeGateway(models=["m"]), facts, model="m")
    assert d.verdict != "ENTER"


def test_an_unconfigured_gateway_yields_a_pass_not_a_crash():
    class _Off:
        configured = False

    d = decide(_Off(), _facts(), model="m")
    assert not d.ok
    assert d.verdict == "PASS"


def test_a_failing_model_yields_a_pass_not_a_crash():
    gateway = FakeGateway(models=["m"])
    gateway.fail("m")
    d = decide(gateway, _facts(), model="m")
    assert not d.ok
    assert d.verdict == "PASS"


def test_a_non_json_answer_is_rejected_rather_than_guessed_at():
    gateway = FakeGateway(models=["m"])
    gateway.make_invalid("m")
    d = decide(gateway, _facts(), model="m")
    assert not d.ok
    assert d.verdict == "PASS"
