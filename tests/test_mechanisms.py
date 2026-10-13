"""Spec 2.0: tape mechanisms, the [NO-NEWS] tag, provenance, rules fallback.

Cause is a label, not a gate. These tests pin the three mechanism detectors that
let a cause-free tape setup clear the gate, the alert tag that says so, the
provenance buckets /stats reports, and the rules-only DECISION block that stands
in when the AI is absent or rejected.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from penny.ai.decision import render_rules_fallback
from penny.alerts import cause_block, format_alert1
from penny.scoring import Metrics
from penny.strategies import Bundle
from penny.strategies.base import StrategyGate, StrategyResult
from penny.strategies.detectors import (MECHANISM_NAMES, short_squeeze_mechanics,
                                        silent_accumulation, sympathy_move)
from penny.tracking import _source_bucket, stats_report


def bundle(**kw):
    d = dict(
        symbol="ABCD", price=1.42, prev_close=1.09, open_price=1.10,
        day_high=1.44, day_low=1.08, cum_volume=9_000_000, pct_vs_close=0.303,
        rise={3: 0.012, 5: 0.035, 15: 0.09},
        volx={3: 9.0, 5: 12.4, 15: 6.0},
        dollar_vol={5: 45_000.0, 15: 210_000.0},
        vwap=1.31, float_shares=18_500_000, short_pct=8.4,
        news=[],
    )
    d.update(kw)
    return Bundle(**d)


def metrics(**kw):
    m = Metrics(symbol="ABCD", ts=time.time(), price=1.42, prev_close=1.09)
    m.volx = {3: 9.0, 5: 12.4, 15: 6.0}
    m.rise = {3: 0.012, 5: 0.035, 15: 0.09}
    m.dollar_vol = {15: 210_000.0}
    m.cum_volume = 9_000_000
    m.vwap = 1.31
    m.score = 70
    m.tier = 2
    m.pct_vs_close = 0.31
    for k, v in kw.items():
        setattr(m, k, v)
    return m


# --- silent_accumulation -----------------------------------------------------

def test_silent_accumulation_fires_on_rising_rotation_above_vwap():
    r = silent_accumulation(bundle())
    assert r.verdict == "PASS"
    assert any("rotation" in x for x in r.reasons)


def test_silent_accumulation_is_silent_when_a_cause_is_named():
    r = silent_accumulation(bundle(news=[{"title": "Acme awarded $12M contract",
                                          "source": "Benzinga", "url": "u",
                                          "age": "2h"}]))
    assert r.verdict == "FAIL"
    assert "a cause is named" in r.failed_on


def test_silent_accumulation_fails_below_vwap():
    assert silent_accumulation(bundle(vwap=1.50)).verdict == "FAIL"


def test_silent_accumulation_fails_on_thin_volume():
    assert silent_accumulation(bundle(volx={3: 1.0, 5: 1.2, 15: 1.0})).verdict == "FAIL"


def test_silent_accumulation_is_insufficient_without_news():
    assert silent_accumulation(bundle(news=None)).verdict == "INSUFFICIENT_DATA"


def test_silent_accumulation_is_insufficient_without_a_float():
    assert silent_accumulation(bundle(float_shares=None)).verdict == "INSUFFICIENT_DATA"


# --- sympathy_move -----------------------------------------------------------

def test_sympathy_move_is_insufficient_without_a_peer_feed():
    """No peer feed is unknown, not absent: never a guess, never a PASS."""
    r = sympathy_move(bundle(sector_peers=None))
    assert r.verdict == "INSUFFICIENT_DATA"


def test_sympathy_move_fires_when_a_peer_outruns_the_target():
    r = sympathy_move(bundle(sector_peers={"AAA": 25.0, "BBB": 12.0}))
    assert r.verdict == "PASS"
    assert any("peer" in x for x in r.reasons)


def test_sympathy_move_fails_when_no_peer_leads():
    assert sympathy_move(bundle(sector_peers={"AAA": 1.0, "BBB": 0.5})).verdict == "FAIL"


def test_sympathy_move_is_silent_when_a_cause_is_named():
    r = sympathy_move(bundle(sector_peers={"AAA": 25.0},
                             news=[{"title": "Acme awarded $12M contract",
                                    "source": "Benzinga", "url": "u", "age": "2h"}]))
    assert r.verdict == "FAIL"


# --- short_squeeze_mechanics -------------------------------------------------

def test_squeeze_mechanics_fires_on_a_crowd_and_a_tape():
    r = short_squeeze_mechanics(bundle(short_pct=8.4, spread_pct=0.6))
    assert r.verdict == "PASS"


def test_squeeze_mechanics_uses_the_spec_2_floor_of_eight_percent():
    assert short_squeeze_mechanics(
        bundle(short_pct=8.0, spread_pct=0.6)).verdict == "PASS"
    assert short_squeeze_mechanics(
        bundle(short_pct=7.0, spread_pct=0.6)).verdict == "FAIL"


def test_squeeze_mechanics_is_insufficient_without_short_data():
    assert short_squeeze_mechanics(
        bundle(short_pct=None)).verdict == "INSUFFICIENT_DATA"


def test_squeeze_mechanics_fails_on_a_wide_spread():
    assert short_squeeze_mechanics(
        bundle(short_pct=12.0, spread_pct=2.5)).verdict == "FAIL"


def test_squeeze_mechanics_is_silent_when_a_cause_is_named():
    r = short_squeeze_mechanics(bundle(short_pct=20.0, spread_pct=0.5,
                                       news=[{"title": "Acme awarded $12M contract",
                                              "source": "Benzinga", "url": "u",
                                              "age": "2h"}]))
    assert r.verdict == "FAIL"


def test_the_three_mechanisms_are_registered_primaries():
    from penny.strategies.gate import PRIMARY
    for name in MECHANISM_NAMES:
        assert name in PRIMARY


# --- [NO-NEWS] tag and the MECHANISM block -----------------------------------

def test_a_cause_free_alert_is_tagged_no_news():
    text = format_alert1(metrics(), no_news=True)
    assert "[NO-NEWS]" in text


def test_an_explained_alert_is_not_tagged_no_news():
    assert "[NO-NEWS]" not in format_alert1(metrics(), no_news=False)


def test_a_cause_free_alert_renders_the_mechanism_not_a_shrug():
    lines = cause_block(metrics(), None, bundle())
    assert lines == ["MECHANISM: silent accumulation "
                     "(float rotation 0.49x, rising, no news in 24h)"]
    assert "none found" not in " ".join(lines)


def test_cause_block_without_news_says_so_honestly():
    assert cause_block(metrics(), None, bundle(news=None)) == ["CAUSE: not checked yet."]


# --- provenance buckets ------------------------------------------------------

def test_source_bucket_splits_ai_rules_and_rejected():
    assert _source_bucket({"ai_source": "ai", "ai_verdict": "ENTER"}) == "agreement"
    assert _source_bucket({"ai_source": "ai", "ai_verdict": "WAIT"}) == "disagreement"
    assert _source_bucket({"ai_source": "rules", "ai_verdict": ""}) == "rules_only"
    assert _source_bucket({"ai_source": "ai_rejected"}) == "ai_rejected"


def test_stats_report_has_a_by_source_section():
    class _T:
        @staticmethod
        def recent(days):
            return [
                {"primary_strategy": "catalyst_volume", "close_pct": 0.12,
                 "ai_source": "ai", "ai_verdict": "ENTER"},
                {"primary_strategy": "catalyst_volume", "close_pct": -0.03,
                 "ai_source": "ai", "ai_verdict": "ENTER"},
                {"primary_strategy": "catalyst_volume", "close_pct": 0.05,
                 "ai_source": "rules", "ai_verdict": ""},
                {"primary_strategy": "catalyst_volume", "close_pct": -0.01,
                 "ai_source": "ai_rejected", "ai_verdict": ""},
            ]

    text = stats_report(_T())
    assert "BY SOURCE" in text
    assert "agreement" in text and "rules_only" in text and "ai_rejected" in text


# --- the rules-only fallback block -------------------------------------------

def _gate(passed=True, score=80.0, blocked_by=None):
    return StrategyGate(passed=passed, gate_score=score, blocked_by=blocked_by,
                        strategies=[StrategyResult(name="catalyst_volume",
                                                   verdict="PASS", score=score)])


def test_rules_fallback_is_a_full_block_with_provenance():
    plan = SimpleNamespace(stop=1.33, entry_low=1.44, entry_high=1.46,
                           overhead=[1.55, 1.70])
    text = render_rules_fallback(metrics(), _gate(), bundle(plan=plan),
                                 source="rules", reason="ai: not configured")
    assert "DECISION (rules-only)" in text
    assert "VERDICT: ENTER" in text
    assert "RATING: STRONG" in text
    assert "LEVELS: stop (structural) 1.33 | overhead 1.55, 1.70 | entry 1.44-1.46" in text
    assert "SOURCE: rules" in text
    assert "REASON:" not in text


def test_rules_fallback_waits_when_the_gate_blocked():
    text = render_rules_fallback(metrics(), _gate(passed=False, blocked_by="EXHAUSTED"),
                                 bundle(), source="rules", reason="ai: not configured")
    assert "VERDICT: NO_TRADE" in text
    assert "gate blocked: EXHAUSTED" in text


def test_a_rejected_report_carries_its_reason():
    text = render_rules_fallback(metrics(), _gate(), bundle(), source="ai_rejected",
                                 reason="non-additive report")
    assert "SOURCE: ai_rejected" in text
    assert "REASON: non-additive report" in text
