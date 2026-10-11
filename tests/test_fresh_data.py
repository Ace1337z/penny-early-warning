"""Freshness: the plan is built from *now*, not from the last cached bar."""

from __future__ import annotations

import time

from penny.enrich import Enricher
from penny.fib import fib_levels, trade_plan


def _bars(count=120, end_age=600, price=1.00):
    """`end_age` seconds old, so the newest bar lags the live quote."""
    now = time.time()
    out = []
    for i in range(count):
        ts = now - end_age - (count - 1 - i) * 60
        out.append({"time": ts, "open": price, "high": price, "low": price,
                    "close": price, "volume": 1000})
    return out


def test_the_live_quote_is_folded_into_the_newest_bar():
    """Without this the EMA/RSI, the Fibonacci swing and the entry/stop were all
    computed from a price the ticker had already left."""
    bars = _bars(end_age=600)
    quote = {"price": 1.25, "ts": time.time()}
    merged = Enricher._live_tail(bars, quote)
    assert len(merged) == len(bars) + 1
    assert merged[-1]["close"] == 1.25
    assert merged[-1]["high"] == 1.25
    # The synthetic bar must not zero the volume-spike test.
    assert merged[-1]["volume"] == 1000
    # The caller's list is not mutated.
    assert len(bars) == 120


def test_candles_that_already_cover_the_quote_are_left_alone():
    now = time.time()
    bars = [{"time": now, "close": 1.0, "open": 1.0, "high": 1.0, "low": 1.0,
             "volume": 10}]
    merged = Enricher._live_tail(bars, {"price": 1.1, "ts": now})
    assert merged is bars


def test_no_quote_leaves_the_candles_alone():
    bars = _bars()
    assert Enricher._live_tail(bars, {"price": None, "ts": time.time()}) is bars


def test_the_swing_and_plan_follow_the_current_price():
    """A stock that ran 1.00 -> 1.50 must have its swing measured over that whole
    move, with the plan sized against today's 1.50 and not the 1.00 it started at."""
    now = time.time()
    bars = []
    for i in range(90):
        ts = now - 600 - (90 - 1 - i) * 60
        px = 1.00 + (0.50 * i / 89)
        bars.append({"time": ts, "open": px, "high": px, "low": px,
                     "close": px, "volume": 1000})
    merged = Enricher._live_tail(bars, {"price": 1.50, "ts": now})
    assert merged[-1]["close"] == 1.50
    levels = fib_levels(min(b["low"] for b in merged), max(b["high"] for b in merged))
    plan = trade_plan(1.50, levels, risk_usd=50, min_rr=2.0)
    assert plan is not None
    assert plan.entry >= 1.0
    assert plan.stop < plan.entry


class _FakeTD:
    configured = True

    def bars(self, symbol, interval="1min", outputsize=300, start=None, end=None):
        now = time.time()
        return [{"time": now - 30, "open": 1, "high": 1, "low": 1,
                 "close": 1, "volume": 5}]


class _FakeAlpacaStale:
    configured = True

    def bars(self, symbol, start, end, **kw):
        now = time.time()
        return [{"time": now - 86400, "open": 9, "high": 9, "low": 9,
                 "close": 9, "volume": 1}]


def test_candle_sources_are_preferred_by_freshness_not_by_order(cfg):
    """A source that answers with yesterday's history must lose to one that has a
    bar from the last minute, even when it is asked second."""
    class Unreachable:
        def bars(self, symbol, days=1, interval="1m"):
            raise AssertionError("a fresh source already answered")

    enr = Enricher(cfg, twelvedata=_FakeTD(), alpaca=_FakeAlpacaStale(),
                   yahoo=Unreachable())
    assert enr._candles("VCIG")[-1]["close"] == 1


def test_stale_sources_still_produce_candles(cfg):
    """When nothing is fresh the newest available set is used, not an empty list."""
    class Stale:
        configured = True

        def bars(self, *a, **kw):
            now = time.time()
            return [{"time": now - 86400, "close": 2.0, "open": 2.0,
                     "high": 2.0, "low": 2.0, "volume": 1}]

    enr = Enricher(cfg, twelvedata=Stale())
    assert enr._candles("VCIG")[-1]["close"] == 2.0


def test_a_failing_source_does_not_stop_the_others(cfg):
    class Broken:
        configured = True

        def bars(self, *a, **kw):
            raise RuntimeError("upstream is down")

    class Good:
        configured = True

        def bars(self, *a, **kw):
            now = time.time()
            return [{"time": now - 30, "close": 3.0, "open": 3.0, "high": 3.0,
                     "low": 3.0, "volume": 5}]

    enr = Enricher(cfg, twelvedata=Broken(), alpaca=Good())
    assert enr._candles("VCIG")[-1]["close"] == 3.0
