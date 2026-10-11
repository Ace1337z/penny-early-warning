"""TwelveData: quote/candle mapping, quotas and graceful degradation."""

from __future__ import annotations

from datetime import datetime, timezone

from penny.sources.base import SourceError
from penny.sources.twelvedata import TwelveDataClient, TwelveDataRejected

QUOTE = {
    "symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ", "currency": "USD",
    "datetime": "2026-10-09", "timestamp": 1791552600,
    "open": "331.70001", "high": "338.60999", "low": "330.70001",
    "close": "336.64001", "volume": "37855000", "previous_close": "340.42001",
    "change": "-3.78000", "percent_change": "-1.11039",
    "average_volume": "36305750", "is_market_open": False,
}

BARS = {
    "status": "ok", "symbol": "AAPL", "interval": "1min",
    "values": [
        {"datetime": "2026-10-09 15:59:00", "open": "336.5", "high": "336.9",
         "low": "336.4", "close": "336.63", "volume": "433896"},
        {"datetime": "2026-10-09 15:58:00", "open": "336.4", "high": "336.6",
         "low": "336.2", "close": "336.5", "volume": "200000"},
    ],
}


def _configured(cfg):
    cfg.set("TWELVEDATA_KEY", "test-key")
    return TwelveDataClient(cfg)


def _client(cfg, payload):
    client = _configured(cfg)
    client._get = lambda path, **params: payload
    return client


def test_an_unconfigured_client_is_simply_off(cfg):
    assert TwelveDataClient(cfg).configured is False
    assert TwelveDataClient(cfg).quote("AAPL") is None
    assert TwelveDataClient(cfg).bars("AAPL") == []


def test_a_quote_maps_into_the_systems_quote_shape(cfg):
    q = _client(cfg, QUOTE).quote("AAPL")
    assert q["symbol"] == "AAPL"
    assert abs(q["price"] - 336.64001) < 1e-6
    assert abs(q["prev_close"] - 340.42001) < 1e-6
    assert q["cum_volume"] == 37_855_000
    assert q["source"] == "twelvedata"


def test_a_quote_without_a_close_is_rejected(cfg):
    assert _client(cfg, {}).quote("AAPL") is None
    assert _client(cfg, {"close": "0"}).quote("AAPL") is None
    assert _client(cfg, QUOTE).quote("") is None


def test_candles_are_returned_oldest_first(cfg):
    """The API answers newest-first and `analyze()`/`anchored_vwap()` index
    `bars[-1]` as now, so leaving the order alone reads the past as present."""
    bars = _client(cfg, BARS).bars("AAPL", "1min", 2)
    assert len(bars) == 2
    assert bars[0]["time"] < bars[1]["time"]
    assert bars[-1]["close"] == 336.63
    assert bars[-1]["volume"] == 433896


def test_bar_timestamps_are_read_as_eastern_not_utc(cfg):
    """A bar labelled 15:59 ET is 19:59 UTC. Parsing it as UTC would shift every
    intraday reading by four hours and corrupt VWAP and every rise window."""
    bar = _client(cfg, BARS).bars("AAPL", "1min", 2)[-1]
    parsed = datetime.fromtimestamp(bar["time"], tz=timezone.utc)
    assert parsed.strftime("%H:%M") == "19:59"
    assert parsed.strftime("%Y-%m-%d") == "2026-10-09"


def test_a_malformed_bar_is_skipped_not_fatal(cfg):
    payload = {"values": [{"datetime": "", "close": "1.0"},
                          {"datetime": "2026-10-09 15:59:00", "close": "2.0"},
                          {"datetime": "not-a-time", "close": "3.0"}]}
    bars = _client(cfg, payload).bars("AAPL")
    assert len(bars) == 1 and bars[0]["close"] == 2.0


def test_the_daily_quota_stops_calls_softly(cfg):
    cfg.set("TWELVEDATA_KEY", "test-key")
    cfg.set("TWELVEDATA_MAX_PER_DAY", "2")
    client = TwelveDataClient(cfg)
    assert client._quota_ok() is True
    assert client._quota_ok() is True
    assert client._quota_ok() is False       # exhausted, no exception


def test_an_error_payload_degrades_to_no_data(cfg):
    client = _configured(cfg)
    client._get = lambda path, **params: {"status": "error", "code": 404,
                                          "message": "symbol not found"}
    assert client.quote("ZZZZ") is None


def test_a_quota_message_surfaces_as_a_rejection(cfg):
    client = _configured(cfg)
    client._quota_ok = lambda: True

    def boom(*a, **kw):
        raise SourceError("twelvedata: HTTP 429")

    client.get_json = boom
    try:
        client._get("/quote", symbol="AAPL")
    except TwelveDataRejected:
        return
    raise AssertionError("an exhausted key must be reported, not swallowed")


def test_health_reports_not_configured(cfg):
    ok, msg = TwelveDataClient(cfg).health()
    assert ok is False
    assert "not configured" in msg


def test_health_reports_ok_on_a_quote(cfg):
    client = _configured(cfg)
    client._get = lambda path, **params: QUOTE
    ok, msg = client.health()
    assert ok is True and "336.64" in msg
