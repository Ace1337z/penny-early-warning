"""Phase 2 reality checks: can the trade be taken, and can it be exited?

Feature 1 reads the market rather than the chart -- spread, 15-minute dollar
volume, average trade size and print count -- and refuses the alert when the
quote cannot be filled. Feature 10 asks the follow-up question: once in, how
long does it take to get $500 out, and at what slip. The Alert 1 delta puts
the same numbers in front of the reader on one line before the AI speaks.

Two rules pin here. INSUFFICIENT_DATA must never read as a pass: a feed that
carries no bid/ask says so and is not graded. And the two hard-block labels
mean different things -- WIDE_SPREAD says the market is quoting wide, ILLIQUID
says nobody is here -- so the gate must pick the right one.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from penny.alerts import book_line, format_alert1
from penny.context import liquidity
from penny.scoring import Metrics
from penny.strategies import spread_liquidity as SL
from penny.strategies.base import Bundle
from penny.strategies.gate import evaluate_gate


NOW = datetime(2026, 10, 12, 10, 30)


def _bundle(**kw):
    b = Bundle(symbol="ABCD", price=1.42, now_et=NOW)
    for key, value in kw.items():
        setattr(b, key, value)
    return b


def _book(bid=1.41, ask=1.43, dollars=1_100_000, trades=800):
    return dict(bid=bid, ask=ask, dollar_vol={15: dollars}, trades_15m=trades)


def _bars(volume, count=15, price=1.42):
    """Fifteen one-minute bars of a given per-minute share volume."""
    return [{"t": 1_760_000_000 + i * 60, "o": price, "h": price, "l": price,
             "c": price, "v": volume} for i in range(count)]


# --- Feature 1: the reality gate ---------------------------------------------

def test_no_bid_or_ask_is_insufficient_and_never_a_pass():
    r = SL.evaluate(_bundle(**_book(bid=None, ask=None)))
    assert r.verdict == "INSUFFICIENT_DATA"
    assert not r.counts_as_pass
    assert "no bid/ask" in " ".join(r.failed_on)


def test_a_fillable_book_passes_with_soft_flags_only():
    r = SL.evaluate(_bundle(**_book()))
    assert r.verdict == "PASS"
    # 1.4% is wide enough to warn about but not wide enough to refuse.
    assert r.flags == ["wide_spread"]
    assert r.failed_on == []


def test_the_wide_spread_is_named_wide_spread_by_the_gate():
    b = _bundle(**_book(bid=1.41, ask=1.53))     # 8.2% spread, liquid otherwise
    r = SL.evaluate(b)
    assert r.verdict == "BLOCK"
    assert r.reasons[0].startswith("spread ")
    gate = evaluate_gate(b)
    assert gate.passed is False
    assert gate.blocked_by == "WIDE_SPREAD"


def test_thin_dollar_volume_is_named_illiquid_by_the_gate():
    b = _bundle(**_book(bid=1.41, ask=1.42, dollars=100_000, trades=500))
    r = SL.evaluate(b)
    assert r.verdict == "BLOCK"
    assert any("15m $100000" in s for s in r.reasons)
    # No spread failure in the reasons, so the label must not claim one.
    gate = evaluate_gate(b)
    assert gate.blocked_by == "ILLIQUID"


def test_a_small_average_trade_size_blocks():
    r = SL.evaluate(_bundle(**_book(dollars=400_000, trades=5_000)))
    assert r.verdict == "BLOCK"
    assert any("avg trade" in s for s in r.reasons)


def test_too_few_prints_block_even_when_the_dollars_look_fine():
    r = SL.evaluate(_bundle(**_book(dollars=900_000, trades=10)))
    assert r.verdict == "BLOCK"
    assert any("trades in 15m" in s for s in r.reasons)


def test_a_feed_without_print_counts_blocks_instead_of_passing():
    r = SL.evaluate(_bundle(**_book(trades=None)))
    assert r.verdict == "BLOCK"
    assert any("no trade count" in s for s in r.reasons)


def test_spread_pct_of_rejects_a_crossed_or_missing_book():
    assert SL.spread_pct_of(1.43, 1.41) is None      # crossed
    assert SL.spread_pct_of(0.0, 1.0) is None         # dead side
    assert SL.spread_pct_of(None, 1.43) is None
    assert SL.spread_pct_of(1.41, None) is None
    assert SL.spread_pct_of(1.41, 1.43) == pytest.approx(1.4084507, rel=1e-4)


def test_average_trade_size_is_notional_over_prints():
    assert SL.avg_trade_size(1_000_000, 500) == pytest.approx(2000.0)
    assert SL.avg_trade_size(100_000, 0) is None
    assert SL.avg_trade_size(None, 500) is None
    assert SL.avg_trade_size(-1.0, 500) is None


# --- Feature 10: exit liquidity ----------------------------------------------

def test_a_deep_book_exits_500_within_a_minute():
    ok, why, seconds, slip = liquidity.passes(_bars(500_000), 1.42)
    assert ok and why == ""
    assert seconds is not None and seconds <= 60
    assert slip is not None and slip <= 1.0


def test_a_thin_book_cannot_get_500_out_in_five_minutes():
    ok, why, seconds, slip = liquidity.passes(_bars(50), 1.42)
    assert not ok
    assert why.startswith("$500 exit needs")
    assert seconds is not None and seconds > 300


def test_no_bars_is_an_absence_of_evidence_not_a_fast_exit():
    assert liquidity.estimate(None, 1.42) == {}
    ok, why, seconds, slip = liquidity.passes(None, 1.42)
    assert ok and why == "" and seconds is None
    assert liquidity.line(None) == ""
    assert liquidity.line({}) == ""


def test_the_exit_line_describes_the_position_the_reader_would_hold():
    line = liquidity.line(liquidity.estimate(_bars(500_000), 1.42))
    assert line.startswith("EXIT LIQUIDITY: $2k exits in ~60s")
    assert "slip" in line


def test_the_gate_names_an_unexittable_stock_illiquid():
    b = _bundle(price=1.42, bars_1m=_bars(50))
    gate = evaluate_gate(b)
    assert gate.passed is False
    assert gate.blocked_by == "ILLIQUID"
    hit = [r for r in gate.strategies if r.name == "exit_liquidity"]
    assert hit and hit[0].verdict == "BLOCK"


# --- Feature 1's Alert 1 delta ------------------------------------------------

def test_alert_one_carries_the_book_and_the_15m_dollars():
    m = Metrics(symbol="ABCD", ts=1_760_000_000, price=1.42, bid=1.41, ask=1.43,
                dollar_vol={15: 1_100_000})
    assert book_line(m) == "bid $1.41 / ask $1.43 (spread 1.4%) | 15m $1,100,000"


def test_a_missing_book_says_so_rather_than_printing_zero():
    m = Metrics(symbol="ABCD", ts=1_760_000_000, price=1.42,
                dollar_vol={15: 500_000})
    assert book_line(m) == "book n/a | 15m $500,000"
    bare = Metrics(symbol="ABCD", ts=1_760_000_000, price=1.42)
    assert book_line(bare) == "book n/a"


def test_the_book_line_is_the_second_line_of_alert_one():
    m = Metrics(symbol="ABCD", ts=1_760_000_000, price=1.42, bid=1.41, ask=1.43,
                dollar_vol={15: 1_100_000}, pct_vs_close=0.042)
    lines = format_alert1(m).split("\n")
    assert lines[0].startswith("ALERT:")
    assert "ABCD" in lines[0]
    assert lines[1].startswith("bid $1.41 / ask $1.43 (spread 1.4%)")
