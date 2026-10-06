"""Market regime and the AI panel's decisive fields."""

from __future__ import annotations

import json

from penny.ai.panel import ModelForecast, aggregate, parse_forecast, reaction_text
from penny.market_context import MarketContext, MarketContextProvider


# --- regime must resolve from Yahoo index levels, not only ETF quotes -------

def test_regime_reads_yahoo_index_levels():
    ctx = MarketContext()
    ctx.index_levels = {"^GSPC": {"price": 7800, "change_pct": 2.02},
                        "^IXIC": {"price": 25400, "change_pct": 3.18},
                        "^VIX": {"price": 17.0, "change_pct": -7.71}}
    assert MarketContextProvider._regime(ctx) == "risk-on"


def test_regime_risk_off_from_index_levels():
    ctx = MarketContext()
    ctx.index_levels = {"^GSPC": {"price": 7000, "change_pct": -1.2},
                        "^IXIC": {"price": 20000, "change_pct": -1.5}}
    assert MarketContextProvider._regime(ctx) == "risk-off"


def test_regime_prefers_quotes_when_present():
    ctx = MarketContext()
    ctx.quotes = {"SPY": {"change_pct": 0.4}, "QQQ": {"change_pct": 0.6},
                  "IWM": {"change_pct": 0.1}}
    assert MarketContextProvider._regime(ctx) == "risk-on"


def test_regime_unknown_without_signal():
    assert MarketContextProvider._regime(MarketContext()) == "unknown"


def test_headline_is_labelled():
    ctx = MarketContext()
    ctx.index_levels = {"^GSPC": {"change_pct": 2.02}}
    ctx.regime = "risk-on"
    line = ctx.headline()
    assert line.startswith("MARKET ")
    assert "RISK-ON" in line


# --- the AI's decisive fields ----------------------------------------------

def _payload(**extra):
    base = {
        "strength": "moderate", "direction": "up", "shape": "burst",
        "durability": "uncertain", "confidence": 0.55, "catalyst": "buyback",
        "why_now": "buyback killed the ATM", "urgency": "wait", "sentiment": 0.6,
        "forecast": {"15m": {"price": 1.2}, "60m": {"price": 1.3},
                     "session_end": {"price": 1.4}},
        "expected_peak": 1.5, "expected_low": 1.1, "market_effect": "risk-on",
        "reasons": ["r1"], "flags": ["low_float"], "change_view": "below VWAP",
    }
    base.update(extra)
    return json.dumps(base)


def test_parser_reads_why_now_and_urgency():
    fc = parse_forecast("m", _payload())
    assert fc.why_now == "buyback killed the ATM"
    assert fc.urgency == "wait"


def test_parser_drops_an_invalid_urgency():
    fc = parse_forecast("m", _payload(urgency="maybe"))
    assert fc.urgency == ""


def test_aggregate_carries_urgency_and_why_now():
    many = [parse_forecast(f"m{i}", _payload(urgency="now")) for i in range(3)]
    agg = aggregate(many)
    assert agg.urgency == "now"
    assert agg.why_now == "buyback killed the ATM"


def test_aggregate_urgency_is_a_majority_vote():
    many = [parse_forecast("a", _payload(urgency="now")),
            parse_forecast("b", _payload(urgency="now")),
            parse_forecast("c", _payload(urgency="avoid"))]
    assert aggregate(many).urgency == "now"


def test_reaction_text_leads_with_the_ai_call():
    agg = aggregate([parse_forecast("m", _payload(urgency="now"))])
    text = reaction_text(agg)
    assert "AI CALL: ENTER NOW" in text
    assert "confidence 0.55 (medium)" in text
    assert "why now:" in text


def test_reaction_text_maps_down_direction_to_avoid():
    fc = parse_forecast("m", _payload(direction="down", urgency=""))
    text = reaction_text(aggregate([fc]))
    assert "AI CALL: AVOID" in text


def test_reaction_text_says_so_when_no_model_answers():
    assert "no model answered" in reaction_text(aggregate([]))
