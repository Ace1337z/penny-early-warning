"""Market regime. The AI panel is gone; the decision report has its own tests."""

from __future__ import annotations

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
