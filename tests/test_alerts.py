"""Alert formatting: the documented shape and limits."""

from __future__ import annotations

import time

from penny.alerts import (MAX_ALERT2, build_ai_facts, format_alert1, format_alert2,
                          verdict_text)
from penny.ai.panel import AggregateForecast, ModelForecast, aggregate
from penny.enrich import Enrichment
from penny.scoring import TIER_CONFIRMED, TIER_EARLY, TIER_NONE, TIER_WATCH, Metrics


def _metrics():
    m = Metrics(symbol="XYZ", ts=time.time(), price=1.227, prev_close=1.0)
    m.rise = {15: 0.05, 60: 0.23}
    m.volx = {15: 10.0}
    m.dollar_vol = {15: 12000}
    m.cum_volume = 113000
    m.vwap = 1.10
    m.minutes_since_low = 116
    m.score = 60
    m.tier = 2
    m.pct_vs_close = 0.227
    return m


def test_alert1_shape():
    text = format_alert1(_metrics(), shariah_line="Shariah: COMPLIANT (halalterminal)",
                         pct_rank=1)
    assert text.startswith("ALERT: EARLY BUILD  XYZ  $1.23")
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


def test_alert2_length_and_disclaimer():
    enr = Enrichment(symbol="XYZ")
    enr.news = [{"title": "headline", "source": "alpaca", "age": "2h", "summary": ""}]
    enr.outlets = 1
    enr.missing = ["candles"]
    text = format_alert2(_metrics(), enr, None, shariah_lines=["Shariah: COMPLIANT"],
                         reaction="REACTION: strong/up/grinder/sustained",
                         price_forecast="PRICE FORECAST: +15m 1.30",
                         ai_running=False)
    assert len(text) <= MAX_ALERT2
    assert "not financial advice" in text
    assert text.startswith("DETAIL ")


def test_alert2_truncates_when_needed():
    enr = Enrichment(symbol="XYZ")
    enr.news = [{"title": "x" * 400, "source": "s", "age": "1h", "summary": "y" * 400}
                for _ in range(20)]
    text = format_alert2(_metrics(), enr, None)
    assert len(text) <= MAX_ALERT2
    assert text.endswith("...(truncated)") or "not financial advice" in text


def test_build_ai_facts_has_core_fields():
    enr = Enrichment(symbol="XYZ")
    enr.news = [{"title": "t", "source": "s", "age": "1h", "summary": "x"}]
    enr.float_shares = 20_000_000
    facts = build_ai_facts(_metrics(), enr, None, session="regular")
    for key in ("symbol", "price", "rise15", "rise60", "volx15", "above_vwap", "tier"):
        assert key in facts
    assert facts["symbol"] == "XYZ"
    assert facts["news"][0]["title"] == "t"


# --- the fast-read alert: NONE explained, verdict first, evidence split -----

def test_tier_none_is_explained_in_setup():
    m = _metrics()
    m.tier = TIER_NONE
    m.rise = {15: 0.01, 60: 0.02}
    m.volx = {15: 1.6}
    text = format_alert2(m, Enrichment(symbol="XYZ"), None, ai_running=False)
    assert "SETUP:" in text
    assert "below the alert bar" in text
    assert "under 3x" in text


def test_verdict_is_early_for_a_building_stock():
    m = _metrics()
    m.tier = TIER_EARLY
    enr = Enrichment(symbol="XYZ")
    assert verdict_text(m, enr).startswith("EARLY")


def test_verdict_avoids_a_fading_stock():
    m = _metrics()
    m.tier = TIER_CONFIRMED
    m.phase = "FADING"
    assert verdict_text(m, Enrichment(symbol="XYZ")).startswith("AVOID")


def test_verdict_waits_when_extended():
    m = _metrics()
    m.tier = TIER_CONFIRMED
    m.phase = "EXTENDED"
    assert verdict_text(m, Enrichment(symbol="XYZ")).startswith("WAIT")


def test_verdict_includes_plan_levels():
    from penny.fib import fib_levels, trade_plan
    m = _metrics()
    m.tier = TIER_EARLY
    enr = Enrichment(symbol="XYZ")
    lv = fib_levels(1.0, 1.5)
    enr.plan = trade_plan(1.05, lv, 50)
    text = verdict_text(m, enr)
    assert "buy " in text and "stop " in text and "T1 " in text


def test_market_backdrop_is_labelled_and_explained():
    class _Market:
        def headline(self):
            return "MARKET SPY +0.4%, QQQ +0.6% | RISK-ON"

    m = _metrics()
    text = format_alert2(m, Enrichment(symbol="XYZ"), _Market(), ai_running=False)
    assert "MARKET BACKDROP" in text
    assert "RISK-ON" in text


def test_catalyst_leads_why_it_moved():
    m = _metrics()
    enr = Enrichment(symbol="XYZ")
    enr.news = [{"title": "Buyback announced", "source": "wire", "age": "2m", "summary": ""}]
    agg = aggregate([ModelForecast(model="m", valid=True, direction="up", strength="strong",
                                   shape="burst", durability="sustained", confidence=0.7,
                                   catalyst="1M share buyback",
                                   forecast={"15m": 1.3, "60m": 1.4, "session_end": 1.5})])
    text = format_alert2(m, enr, None, agg=agg, reaction="REACTION: x", ai_running=False)
    assert "WHY IT MOVED" in text
    assert "CATALYST: 1M share buyback" in text
    # The headline shown under WHY is not repeated under the evidence list.
    assert text.count("Buyback announced") == 1


def test_html_alert_escapes_dynamic_text():
    m = _metrics()
    enr = Enrichment(symbol="XYZ")
    enr.news = [{"title": "A & B <script>", "source": "s", "age": "1h", "summary": ""}]
    text = format_alert2(m, enr, None, ai_running=False, html=True)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp;" in text


def test_html_alert_uses_bold_and_code():
    m = _metrics()
    text = format_alert1(m, html=True)
    assert "<b>" in text
    text2 = format_alert2(m, Enrichment(symbol="XYZ"), None, ai_running=False, html=True)
    assert "<b>" in text2 and "SETUP" in text2


def test_check_layout_has_pulse_meter_and_sections():
    """The /check body reads like /market: arrow, pulse strip, meter, sections."""
    m = _metrics()
    text = format_alert2(m, Enrichment(symbol="XYZ"), None, ai_running=False)
    head = text.splitlines()[0]
    assert "\U0001F7E2" in head                      # direction arrow
    assert "vs close" in head
    assert "15m" in text and "60m" in text and "vol 10.0x" in text   # pulse strip
    assert "\u2588" in text                          # score meter
    assert "above VWAP" in text and "below VWAP" not in text.split("SETUP")[0]
    for section in ("VERDICT", "WHY IT MOVED", "EVIDENCE"):
        assert f"\u2500\u2500 {section}" in text     # divider-labelled section


def test_check_pulse_reflects_a_below_vwap_stock():
    m = _metrics()
    m.vwap = m.price + 0.10            # price under VWAP
    text = format_alert2(m, Enrichment(symbol="XYZ"), None, ai_running=False)
    assert "below VWAP" in text


def test_plain_and_html_carry_the_same_facts():
    m = _metrics()
    enr = Enrichment(symbol="XYZ")
    enr.news = [{"title": "headline", "source": "s", "age": "1h", "summary": ""}]
    plain = format_alert2(m, enr, None, ai_running=False)
    html = format_alert2(m, enr, None, ai_running=False, html=True)
    for token in ("VERDICT", "WHY IT MOVED", "SETUP", "EVIDENCE"):
        assert token in plain and token in html
    # The plan and Fibonacci blocks appear in both forms when the data exists.
    from penny.fib import fib_levels, trade_plan
    m2 = _metrics()
    enr2 = Enrichment(symbol="XYZ")
    lv = fib_levels(1.0, 1.5)
    enr2.fib = lv
    enr2.plan = trade_plan(1.05, lv, 50)
    plain2 = format_alert2(m2, enr2, None, ai_running=False)
    html2 = format_alert2(m2, enr2, None, ai_running=False, html=True)
    for token in ("PLAN", "FIB swing"):
        assert token in plain2 and token in html2
