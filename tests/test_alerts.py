"""Alert formatting: the documented shape and limits.

The alert is deliberately lean (spec sections 10 and 11): no TECH block, no
Fibonacci grid, no standalone PLAN block, no standalone VERDICT heading. The
verdict travels inside the AI DECISION block next to the levels that justify it.
"""

from __future__ import annotations

import time

from penny.alerts import (MAX_ALERT2, build_ai_facts, cause_block, format_alert1,
                           format_alert2, gate_block)
from penny.enrich import Enrichment
from penny.scoring import TIER_CONFIRMED, TIER_EARLY, TIER_NONE, Metrics
from penny.strategies import Bundle, evaluate_gate


def _metrics():
    m = Metrics(symbol="XYZ", ts=time.time(), price=1.227, prev_close=1.0)
    m.rise = {3: 0.012, 5: 0.035, 15: 0.05, 60: 0.23}
    m.volx = {3: 9.0, 5: 12.4, 15: 10.0}
    m.dollar_vol = {15: 12000}
    m.cum_volume = 113000
    m.vwap = 1.10
    m.minutes_since_low = 116
    m.score = 60
    m.tier = 2
    m.pct_vs_close = 0.227
    return m


def _bundle(**kw):
    base = dict(symbol="XYZ", price=1.227, prev_close=1.0, open_price=1.01,
                day_high=1.24, day_low=0.99, cum_volume=113_000,
                pct_vs_close=0.227, rise={3: 0.012, 5: 0.035, 15: 0.05},
                volx={3: 9.0, 5: 12.4, 15: 10.0},
                dollar_vol={5: 900.0, 15: 12_000.0}, vwap=1.10,
                minutes_since_low=116.0, float_shares=20_000_000, short_pct=12.5,
                dollar_level=1.00,
                news=[{"title": "Buyback announced", "source": "wire",
                       "url": "https://example.invalid/a", "age": "2m"}])
    base.update(kw)
    return Bundle(**base)


# --- alert 1 ----------------------------------------------------------------

def test_alert1_shape():
    text = format_alert1(_metrics(), shariah_line="Shariah: COMPLIANT (halalterminal)",
                         pct_rank=1)
    assert text.startswith("ALERT: EARLY  XYZ  $1.23")
    assert "+22.7% vs close" in text
    assert "vol 10.0x normal" in text
    assert "#1 gainer" in text
    assert "Shariah: COMPLIANT" in text


def test_alert1_includes_phase_and_resume():
    m = _metrics()
    m.phase = "FADING"
    text = format_alert1(m, resumed=True)
    assert "[FADING]" in text
    assert "RESUMED FROM HALT" in text


def test_alert1_names_the_strategy_that_recognised_the_setup():
    """Gate-passers know *which* setup fired, not just that something did."""
    text = format_alert1(_metrics(), gate=evaluate_gate(_bundle()))
    assert "STRATEGY:" in text
    assert "no strategy" not in text.lower()


# --- alert 2 ----------------------------------------------------------------

def test_alert2_length_and_disclaimer():
    enr = Enrichment(symbol="XYZ")
    enr.news = [{"title": "headline", "source": "alpaca", "age": "2h", "summary": ""}]
    enr.outlets = 1
    enr.missing = ["candles"]
    text = format_alert2(_metrics(), enr, None, shariah_lines=["Shariah: COMPLIANT"])
    assert len(text) <= MAX_ALERT2
    assert "not investment advice" in text.lower()
    assert text.startswith("DETAIL ")


def test_alert2_truncates_when_needed():
    enr = Enrichment(symbol="XYZ")
    enr.news = [{"title": "x" * 400, "source": "s", "age": "1h", "summary": "y" * 400}
                for _ in range(20)]
    text = format_alert2(_metrics(), enr, None)
    assert len(text) <= MAX_ALERT2
    assert text.endswith("...(truncated)") or "not investment advice" in text.lower()


def test_alert2_is_lean_no_tech_fib_plan_or_standalone_verdict():
    """Spec sections 10 and 11: those four blocks are gone from the reader's view."""
    from penny.fib import fib_levels, trade_plan
    enr = Enrichment(symbol="XYZ")
    enr.fib = fib_levels(1.0, 1.5)
    enr.plan = trade_plan(1.05, enr.fib, 50)
    enr.technicals = type("T", (), {"summary_text": lambda self: "RSI 60"})()
    enr.verification = {"status": "ok"}
    text = format_alert2(_metrics(), enr, None, bundle=_bundle(),
                         decision="VERDICT: ENTER\nENTRY 1.22-1.24\nSTOP 1.15")
    assert "\nTECH" not in text
    assert "FIB" not in text
    assert "\nPLAN" not in text
    # The verdict is not a section of its own any more.
    assert "\u2500\u2500 VERDICT" not in text
    assert "\u2500\u2500 AI DECISION" in text


def test_build_ai_facts_has_core_fields():
    enr = Enrichment(symbol="XYZ")
    enr.news = [{"title": "t", "source": "s", "age": "1h", "summary": "x"}]
    enr.float_shares = 20_000_000
    facts = build_ai_facts(_metrics(), enr, None, session="regular")
    for key in ("symbol", "price", "rise3", "rise5", "rise15", "volx15", "above_vwap",
                "tier"):
        assert key in facts
    assert facts["symbol"] == "XYZ"


def test_build_ai_facts_carries_the_gate_and_the_cause():
    """The AI sees the gate verdict and the one named cause, not a headline dump."""
    bundle = _bundle()
    facts = build_ai_facts(_metrics(), Enrichment(symbol="XYZ"), None,
                           session="regular", gate=evaluate_gate(bundle),
                           bundle=bundle)
    assert "passed" in facts["gate"]
    # The cause is carried as rendered lines, so the AI reads the same sentence
    # the reader does rather than ranking the headlines a second time.
    assert facts["cause"][0].startswith("CAUSE:")


# --- the lean read ----------------------------------------------------------

def test_tier_none_is_explained_in_setup():
    m = _metrics()
    m.tier = TIER_NONE
    m.rise = {3: 0.001, 5: 0.002, 15: 0.01, 60: 0.02}
    m.volx = {3: 0.9, 5: 1.1, 15: 1.6}
    text = format_alert2(m, Enrichment(symbol="XYZ"), None)
    assert "SETUP:" in text
    assert "below the alert bar" in text
    assert "under 3x" in text


def test_gate_block_names_the_setup_and_the_secondary_markers():
    gate = evaluate_gate(_bundle())
    lines = gate_block(gate)
    assert lines
    assert lines[0].startswith("STRATEGY:")
    assert "TRIGGER:" not in lines[0]     # a plain pass carries no trigger


def test_market_backdrop_is_labelled_and_explained():
    class _Market:
        def headline(self):
            return "MARKET SPY +0.4%, QQQ +0.6% | RISK-ON"

    text = format_alert2(_metrics(), Enrichment(symbol="XYZ"), _Market())
    assert "MARKET BACKDROP" in text
    assert "RISK-ON" in text


def test_cause_leads_why_it_moved_and_names_one_reason():
    """Cause-first: exactly one cause, with outlet, age and confidence."""
    bundle = _bundle()
    text = format_alert2(_metrics(), Enrichment(symbol="XYZ"), None, bundle=bundle)
    assert "WHY IT MOVED" in text
    assert "CAUSE:" in text
    # The cause block comes before the evidence block.
    assert text.index("WHY IT MOVED") < text.index("EVIDENCE")
    assert text.count("CAUSE:") == 1


def test_no_cause_says_none_found_instead_of_inventing_one():
    """A news dump is not a cause. Saying so beats attaching a plausible PR."""
    bundle = _bundle(news=[{"title": "Company provides update", "source": "Yahoo",
                            "url": "u", "age": "5h"}])
    lines = cause_block(_metrics(), Enrichment(symbol="XYZ"), bundle)
    assert lines[0].startswith("CAUSE: none found")


def test_a_fresh_hard_catalyst_is_the_named_cause():
    bundle = _bundle(news=[{"title": "Acme Corp awarded $12M contract",
                            "source": "Benzinga", "url": "https://example.invalid/a",
                            "age": "2m"}])
    lines = cause_block(_metrics(), Enrichment(symbol="XYZ"), bundle)
    assert lines[0].startswith("CAUSE:")
    assert "awarded $12M" in lines[0]


def test_html_alert_escapes_dynamic_text():
    enr = Enrichment(symbol="XYZ")
    enr.news = [{"title": "A & B <script>", "source": "s", "age": "1h", "summary": ""}]
    enr.filings = [{"form": "8-K", "date": "9/29/2026", "items": "A & B <script>"}]
    text = format_alert2(_metrics(), enr, None, html=True)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp;" in text


def test_html_alert_uses_bold_and_code():
    m = _metrics()
    text = format_alert1(m, html=True)
    assert "<b>" in text
    text2 = format_alert2(m, Enrichment(symbol="XYZ"), None, html=True)
    assert "<b>" in text2 and "SETUP" in text2


def test_detail_layout_has_pulse_strip_and_labelled_sections():
    m = _metrics()
    text = format_alert2(m, Enrichment(symbol="XYZ"), None)
    head = text.splitlines()[0]
    assert head.startswith("DETAIL  ") and "XYZ" in head
    assert "PULSE:" in text
    assert "vs close" in head
    for section in ("WHY IT MOVED", "EVIDENCE", "AI DECISION"):
        assert f"\u2500\u2500 {section}" in text


def test_a_missing_ai_call_still_leaves_a_usable_alert():
    """No decision call must not mean no message: the strategy block stands."""
    text = format_alert2(_metrics(), Enrichment(symbol="XYZ"), None,
                         gate=evaluate_gate(_bundle()), bundle=_bundle())
    assert "AI DECISION" in text
    assert "AI unavailable" in text
    assert "STRATEGY:" in text


def test_detail_pulse_reflects_a_below_vwap_stock():
    m = _metrics()
    m.vwap = m.price + 0.10            # price under VWAP
    text = format_alert2(m, Enrichment(symbol="XYZ"), None)
    assert "below VWAP" in text


def test_plain_and_html_carry_the_same_facts():
    m = _metrics()
    enr = Enrichment(symbol="XYZ")
    enr.news = [{"title": "headline", "source": "s", "age": "1h", "summary": ""}]
    bundle = _bundle()
    plain = format_alert2(m, enr, None, bundle=bundle)
    html = format_alert2(m, enr, None, bundle=bundle, html=True)
    for token in ("WHY IT MOVED", "SETUP", "EVIDENCE", "AI DECISION"):
        assert token in plain and token in html


def test_evidence_groups_are_separated_by_blank_lines():
    """Filings, short interest and Shariah must not run together."""
    enr = Enrichment(symbol="XYZ")
    enr.verification = {"status": "ok"}
    enr.news = [{"title": "first", "source": "s", "age": "1h", "summary": ""},
                {"title": "second", "source": "s", "age": "2h", "summary": ""}]
    enr.filings = [{"form": "8-K", "date": "9/29/2026", "items": ""}]
    enr.short_interest = [{"short_float_pct": 12.5, "date": "9/1", "source": "finviz"}]
    enr.insiders = [{"date": "9/1", "owner": "DOE", "transaction": "Buy", "value": "$1"}]
    lines = format_alert2(_metrics(), enr, None,
                          shariah_lines=["Shariah: COMPLIANT"]).split("\n")
    for heading in ("FILINGS", "SHORT/FLOAT", "INSIDERS"):
        idx = next(i for i, ln in enumerate(lines) if heading in ln)
        assert lines[idx - 1] == "", f"{heading} should be preceded by a blank line"


def test_confirmed_tier_never_produces_a_short_instruction():
    """Long-only: no *trade instruction* may be a short. Short float is data."""
    m = _metrics()
    m.tier = TIER_CONFIRMED
    text = format_alert2(m, Enrichment(symbol="XYZ"), None, bundle=_bundle()).lower()
    for banned in ("sell short", "go short", "short entry", "short below",
                   "short above", "fade the", "short squeeze play"):
        assert banned not in text
