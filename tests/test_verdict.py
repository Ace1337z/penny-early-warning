"""The portfolio-manager verdict: gates, tradeability, and the R:R floor."""

from __future__ import annotations

import json

from penny.ai.panel import aggregate, parse_forecast
from penny.ai.verdict import (VERDICT_ENTER, VERDICT_PASS, VERDICT_WAIT, data_conflict,
                              decide, plan_conflict, verdict_text)
from penny.fib import fib_levels, trade_plan


def _payload(**over):
    data = {
        "strength": "strong", "direction": "up", "shape": "burst",
        "durability": "sustained", "confidence": 0.7, "verdict": "ENTER",
        "catalyst": "offering", "why_now": "volume is arriving", "urgency": "now",
        "sentiment": 0.4,
        "forecast": {"15m": {"price": 1.2}, "60m": {"price": 1.3},
                     "session_end": {"price": 1.25}},
        "expected_peak": 1.5, "expected_low": 1.0,
        "reasons": ["r1"], "flags": [], "change_view": "below VWAP",
    }
    data.update(over)
    return json.dumps(data)


def _agg(**over):
    return aggregate([parse_forecast("m", _payload(**over))])


def _facts(**over):
    facts = {"price": 1.0, "volx15": 4.0, "from_high": -2.0}
    facts.update(over)
    return facts


# --- the headline case ------------------------------------------------------

def test_strong_momentum_but_extended_price_is_wait_not_enter():
    """The spec's example: everything reads bullish and the stock has already
    run past what the panel expected. Direction is up; the entry is not there."""
    agg = _agg(expected_peak=0.95)          # price 1.00 is already past the peak
    decide(agg, _facts(), min_rr=2.0)
    assert agg.direction == "up"
    assert agg.strength == "strong"
    assert agg.verdict == VERDICT_WAIT
    assert "expected peak" in agg.verdict_reason


def test_a_fresh_move_with_room_left_can_enter():
    agg = _agg(expected_peak=1.50)         # price 1.00 is 33% below the peak
    decide(agg, _facts(), min_rr=2.0)
    assert agg.verdict == VERDICT_ENTER


# --- hard gates -------------------------------------------------------------

def test_reward_risk_below_the_minimum_is_pass_with_no_recommendation():
    levels = fib_levels(1.00, 1.02)        # a 2% swing: nothing worth trading
    plan = trade_plan(1.02, levels, risk_usd=50, min_rr=2.0)
    assert plan is not None
    assert plan.viable is False
    assert plan.reward_risk is not None and plan.reward_risk < 2.0
    facts = _facts(trade_plan=plan.as_dict())
    assert "below the 2:1 minimum" in plan_conflict(facts, 2.0)
    agg = _agg()
    decide(agg, facts, min_rr=2.0)
    assert agg.verdict == VERDICT_PASS
    assert "reward:risk" in agg.verdict_reason


def test_a_viable_plan_does_not_block_the_verdict():
    # A pullback entry deep in a wide swing: the 38.2-50% zone sits far enough
    # below the 127.2% extension that T1 pays more than twice the risk.
    levels = fib_levels(1.00, 2.00)
    plan = trade_plan(1.30, levels, risk_usd=50, min_rr=2.0)
    assert plan.viable is True
    facts = _facts(trade_plan=plan.as_dict())
    assert plan_conflict(facts, 2.0) == ""
    agg = _agg()
    decide(agg, facts, min_rr=2.0)
    assert agg.verdict == VERDICT_ENTER


def test_conflicting_quote_and_verified_price_forces_pass_data_conflict():
    facts = _facts(finviz={"status": "mismatch", "price": 1.40})
    assert "5%" in data_conflict(facts)
    agg = _agg()
    decide(agg, facts, min_rr=2.0)
    assert agg.verdict == VERDICT_PASS
    assert agg.verdict_reason.startswith("DATA_CONFLICT:")
    assert agg.data_conflict


def test_a_missing_price_is_a_data_conflict():
    assert data_conflict({"price": None}) != ""
    assert data_conflict({"price": 0}) != ""


def test_no_model_answered_is_wait_not_a_guess():
    agg = aggregate([])
    decide(agg, _facts(), min_rr=2.0)
    assert agg.verdict == VERDICT_WAIT
    assert "no model answered" in agg.verdict_reason


# --- the gate only tightens -------------------------------------------------

def test_the_rules_can_downgrade_the_panel_but_never_upgrade_it():
    agg = _agg(verdict="PASS")
    decide(agg, _facts(expected_peak=1.5), min_rr=2.0)   # everything else perfect
    assert agg.verdict == VERDICT_PASS


def test_a_down_direction_is_never_enter():
    agg = _agg(direction="down", urgency="avoid")
    decide(agg, _facts(), min_rr=2.0)
    assert agg.verdict == VERDICT_PASS


def test_a_move_with_no_volume_participation_is_wait():
    agg = _agg()
    decide(agg, _facts(volx15=0.4), min_rr=2.0)
    assert agg.verdict == VERDICT_WAIT
    assert "15-minute average" in agg.verdict_reason


def test_a_stock_far_off_its_high_is_wait():
    agg = _agg()
    decide(agg, _facts(from_high=-28.0), min_rr=2.0)
    assert agg.verdict == VERDICT_WAIT
    assert "off the day's high" in agg.verdict_reason


# --- rendering --------------------------------------------------------------

def test_the_pm_line_names_the_verdict_and_one_reason():
    agg = _agg()
    decide(agg, _facts(volx15=0.4), min_rr=2.0)
    line = verdict_text(agg)
    assert line.startswith("PM: WAIT |")
    assert line.count("\n") == 0
