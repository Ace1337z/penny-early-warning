"""Alert formatting: the documented shape and limits."""

from __future__ import annotations

import time

from penny.alerts import MAX_ALERT2, build_ai_facts, format_alert1, format_alert2
from penny.enrich import Enrichment
from penny.scoring import Metrics


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
