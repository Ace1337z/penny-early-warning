"""Finviz feed: intraday signals, extended hours, volume units, column coverage."""

from __future__ import annotations

from penny.config import DEFAULTS
from penny.sources.finviz import (INTRADAY_BASES, PERF_COLUMNS, SIGNALS,
                                  avg_volume_shares, intraday_signal_ok)


def _client(cfg):
    from penny.sources.finviz import FinvizClient
    cfg.set("FINVIZ_TOKEN", "test-token")
    return FinvizClient(cfg)


# --- configured columns -----------------------------------------------------

def test_default_columns_include_the_quote_fields():
    """The quote shape reads Volume/Prev Close/after-hours; they must be requested."""
    cols = {c.strip() for c in DEFAULTS["FINVIZ_COLUMNS"].split(",")}
    # 67 Volume, 71/72 after-hours, 81 Prev Close, 86/87/88 O/H/L, 90..99 intraday perf.
    for cid in ("67", "71", "72", "81", "86", "87", "88", "95"):
        assert cid in cols
    # The old default stopped at 66 (Change) and never asked for Volume.
    assert "66" in cols


def test_default_view_is_the_custom_view():
    """The Overview view (111) ignores `c=` and omits float/avg-volume; the feed and
    verify() must both use the custom view so the column set applies."""
    assert DEFAULTS["FINVIZ_VIEW"] == "152"
    assert int(DEFAULTS["FINVIZ_MAX_PER_MIN"]) >= 30  # ~29 calls/min in-session


def test_default_intraday_signals_are_top_gainers_only():
    for name in DEFAULTS["FINVIZ_INTRADAY_SIGNALS"].split(","):
        code = SIGNALS[name.strip()]
        assert intraday_signal_ok(code), code


# --- intraday signal support ------------------------------------------------

def test_only_top_gainers_and_losers_honour_a_timeframe():
    for base in INTRADAY_BASES:
        for tf in ("1m", "5m", "15m", "30m", "1h"):
            assert intraday_signal_ok(f"{base}_{tf}")
    # These silently return the whole universe with a timeframe, so they are refused.
    assert not intraday_signal_ok("ta_unusualvolume_5m")
    assert not intraday_signal_ok("ta_mostactive_1m")
    assert not intraday_signal_ok("ta_newhigh_5m")


def test_signals_without_a_timeframe_are_fine():
    assert intraday_signal_ok("ta_topgainers")
    assert intraday_signal_ok("it_latestbuys")


def test_movers_merges_intraday_signals(cfg, monkeypatch):
    client = _client(cfg)
    seen: list[str] = []

    def fake_screener(**kw):
        seen.append(kw.get("signal", ""))
        return [{"Ticker": "T" + str(len(seen)), "Price": "1.00"}]

    monkeypatch.setattr(client, "screener", fake_screener)
    cfg.set("FINVIZ_SIGNALS", "top_gainers")
    cfg.set("FINVIZ_INTRADAY_SIGNALS", "top_gainers_1m,top_gainers_5m")
    rows = client.movers("regular", top_n=10)
    assert seen == ["ta_topgainers", "ta_topgainers_1m", "ta_topgainers_5m"]
    assert len(rows) == 3


def test_movers_skips_signals_a_timeframe_does_not_support(cfg, monkeypatch):
    client = _client(cfg)
    seen: list[str] = []
    monkeypatch.setattr(client, "screener",
                        lambda **kw: (seen.append(kw.get("signal")), [])[1])
    cfg.set("FINVIZ_SIGNALS", "unusual_volume")
    cfg.set("FINVIZ_INTRADAY_SIGNALS", "top_gainers_5m")
    client.movers("regular")
    # The volume screen has no timeframe; only the explicit intraday signal runs.
    assert seen == ["ta_unusualvolume", "ta_topgainers_5m"]
    cfg.set("FINVIZ_SIGNALS", "")
    cfg.set("FINVIZ_INTRADAY_SIGNALS", "unusual_volume_5m")
    seen.clear()
    client.movers("regular")
    assert seen == []


def test_movers_is_empty_when_market_is_closed(cfg):
    client = _client(cfg)
    assert client.movers("closed") == []


# --- view coherence ---------------------------------------------------------

def test_screener_coerces_a_fixed_view_when_columns_are_requested(cfg, monkeypatch):
    """A view that ignores c= (e.g. the Overview 111) must not drop the custom
    columns; otherwise Volume is missing and no tier or build can fire."""
    client = _client(cfg)
    seen: list[dict] = []

    def fake_fetch(link, **params):
        seen.append(params)
        return "Ticker,Price\nAAPL,1.00\n"

    monkeypatch.setattr(client, "fetch", fake_fetch)
    cfg.set("FINVIZ_VIEW", "111")
    client.screener(columns="0,67")
    assert seen[-1]["v"] == "152"
    # Without columns (e.g. a plain mover screen) the configured view is kept.
    client.screener()
    assert seen[-1]["v"] == "111"


def test_screener_keeps_an_explicit_custom_view(cfg, monkeypatch):
    client = _client(cfg)
    seen: list[dict] = []

    def fake_fetch(link, **params):
        seen.append(params)
        return "Ticker,Price\nAAPL,1.00\n"

    monkeypatch.setattr(client, "fetch", fake_fetch)
    client.screener(columns="0,67", view="152")
    assert seen[-1]["v"] == "152"


# --- required columns are always requested ----------------------------------

def test_merge_columns_adds_the_quote_fields():
    """A stale config.env can drop Volume/after-hours; the feed must still ask."""
    from penny.sources.finviz import REQUIRED_COLUMNS, merge_columns
    merged = {int(c) for c in merge_columns("0,1,66").split(",")}
    assert {0, 1, 66} <= merged
    assert set(REQUIRED_COLUMNS) <= merged
    assert 67 in merged and 81 in merged and 95 in merged


def test_screener_requests_the_required_columns(cfg, monkeypatch):
    """Even with the old column set, the request carries Volume and Prev Close."""
    client = _client(cfg)
    seen: list[dict] = []

    def fake_fetch(link, **params):
        seen.append(params)
        return "Ticker,Price\nAAPL,1.00\n"

    monkeypatch.setattr(client, "fetch", fake_fetch)
    client.screener(columns="0,1,66")
    sent = {c for c in seen[-1]["c"].split(",")}
    assert "67" in sent  # Volume
    assert "81" in sent  # Prev Close
    assert "0" in sent


# --- intraday screens are never starved by a list-wide cap ------------------

def test_movers_cap_does_not_hide_the_intraday_screens(cfg, monkeypatch):
    """The daily screens alone can exceed top_n; the intraday screens run last
    and must still be reached."""
    client = _client(cfg)
    calls: list[str] = []

    def fake_screener(**kw):
        sig = kw.get("signal", "")
        calls.append(sig)
        ticker = "D" if "1m" not in sig else "M"
        return [{"Ticker": f"{ticker}{i}", "Price": "1.00"} for i in range(5)]

    monkeypatch.setattr(client, "screener", fake_screener)
    cfg.set("FINVIZ_SIGNALS", "top_gainers")
    cfg.set("FINVIZ_INTRADAY_SIGNALS", "top_gainers_1m")
    rows = client.movers("regular", top_n=3)
    # Both screens ran despite the small cap, and each contributed its own rows.
    assert calls == ["ta_topgainers", "ta_topgainers_1m"]
    tickers = {r["Ticker"] for r in rows}
    assert any(t.startswith("D") for t in tickers)
    assert any(t.startswith("M") for t in tickers)


# --- extended-hours prices --------------------------------------------------

def _row(**kw):
    base = {"Ticker": "VCIG", "Price": "1.41", "Change": "54.77%",
            "Volume": "20235178", "Average Volume": "583.28",
            "Prev Close": "0.91", "After-Hours Close": "1.80",
            "After-Hours Change": "27.52%"}
    base.update(kw)
    return base


def test_after_hours_price_used_in_post_session(cfg):
    client = _client(cfg)
    quote = client.normalize_row(_row(), "post")
    # 1.41 close carried +27.52% after hours.
    assert abs(quote["price"] - 1.41 * 1.2752) < 1e-6
    # % vs close uses the explicit previous close, not the regular Change.
    assert abs(quote["prev_close"] - 0.91) < 1e-9


def test_regular_session_ignores_after_hours_columns(cfg):
    client = _client(cfg)
    quote = client.normalize_row(_row(), "regular")
    assert quote["price"] == 1.41
    assert abs(quote["prev_close"] - 1.41 / 1.5477) < 1e-6


def test_premarket_keeps_finviz_price(cfg):
    """Pre-market Finviz already folds the pre-market print into Price; the
    after-hours columns would be yesterday's, so they must not be used."""
    client = _client(cfg)
    quote = client.normalize_row(_row(Price="1.90", Change="110.0%"), "pre")
    assert quote["price"] == 1.90


def test_extended_hours_can_be_disabled(cfg):
    client = _client(cfg)
    cfg.set("FINVIZ_EXTENDED_HOURS", "0")
    quote = client.normalize_row(_row(), "post")
    assert quote["price"] == 1.41


def test_intraday_performance_is_exposed(cfg):
    client = _client(cfg)
    row = _row()
    row["Performance (5 Minutes)"] = "4.20%"
    quote = client.normalize_row(row, "regular")
    assert quote["intraday_perf"]["5"] == 4.2
    assert PERF_COLUMNS[0] == (5, "Performance (5 Minutes)")


# --- volume units -----------------------------------------------------------

def test_average_volume_is_converted_from_thousands():
    # Finviz reports 583.28 meaning 583.28K.
    assert avg_volume_shares({"Average Volume": "583.28"}) == 583_280.0
    # Simulation doubles provide raw shares under the other name.
    assert avg_volume_shares({"Avg Volume": "96000"}) == 96_000.0
    assert avg_volume_shares({}) is None


def test_quote_volume_and_avg_volume_are_raw_shares(cfg):
    client = _client(cfg)
    row = _row()
    row["Relative Volume"] = "34.69"
    quote = client.normalize_row(row, "regular")
    assert quote["cum_volume"] == 20_235_178
    assert quote["avg_volume"] == 583_280.0
    assert quote["rel_volume"] == 34.69


def test_volume_survives_when_finviz_omits_change(cfg):
    client = _client(cfg)
    quote = client.normalize_row(_row(Change=""), "regular")
    assert quote["cum_volume"] == 20_235_178
    assert quote["prev_close"] == 0.91  # falls back to Prev Close
