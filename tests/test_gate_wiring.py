"""The `Metrics` -> Bundle seam, and the gate's right to say no.

Nothing in the suite handed a real `Metrics` to `metrics_to_bundle`, so the
wiring read a field `Metrics` never had and every gate evaluation raised
AttributeError. The engine caught that, logged it and returned no gate at all,
and because `cycle()` read "no gate" as "nothing to enforce" the alert then went
out ungated -- while `/check`, which calls the gate directly, failed outright.
The same pass wrote its rejection record with an unbound name, so the first
rejection raised too.

Each of those is pinned here, along with the two rules that keep a mandatory
gate safe: an unevaluable gate fails closed rather than open, and a verdict the
poll loop cannot reach (no candles, no news) does not veto -- while the tier bar
keeps filtering, because it is all that can decide at that point.
"""

from __future__ import annotations

import time

import pytest

from penny.enrich import Enrichment
from penny.scoring import Metrics, Scorer, TIER_CONFIRMED, TIER_NONE
from penny.strategies.base import StrategyGate
from penny.strategies.wiring import metrics_to_bundle, run_gate
from penny.util import to_et


def _engine(cfg, tmp_path):
    """A real Engine with a fake Telegram, as the alert tests build one."""
    from penny.engine import Engine
    from penny.store import open_journal, open_state
    from penny.telegram import FakeTelegram
    db = open_state(tmp_path / "state.db")
    journal = open_journal(tmp_path / "journal.db")
    engine = Engine(cfg, db, journal, FakeTelegram(cfg))
    engine.scorer.cycles = engine.scorer.warmup_cycles + 1
    return engine


def _real_metrics(cfg, symbol="ABCD"):
    """A Metrics the production scorer built, not a hand-made stand-in.

    The bug these tests exist for was invisible to hand-built Bundles and to
    hand-built Metrics alike: only the real object carried the field shape the
    wiring actually reads.
    """
    scorer = Scorer(cfg)
    scorer.set_avg_volume(symbol, 400_000.0)
    now = time.time()
    m = None
    for i in range(20):
        price = 1.10 + i * 0.012
        out = scorer.compute({
            "symbol": symbol, "ts": now + i * 60, "price": price,
            "cum_volume": 200_000 + i * 140_000, "prev_close": 1.09,
            "bid": round(price - 0.005, 4), "ask": round(price + 0.005, 4),
            "market_cap": 26_000_000, "float_shares": 18_500_000,
            "high_price": price + 0.02,
        }, "regular")
        if out is not None:
            m = out
    assert m is not None, "the scorer produced no metrics"
    assert m.age_seconds > 0, "the symbol was not tracked long enough"
    return m


def _bare_metrics(cfg, symbol="WXYZ"):
    """A Finviz-only quote: no bid, no ask, so the book cannot be judged."""
    scorer = Scorer(cfg)
    scorer.set_avg_volume(symbol, 400_000.0)
    now = time.time()
    m = None
    for i in range(12):
        out = scorer.compute({
            "symbol": symbol, "ts": now + i * 60, "price": 1.20 + i * 0.01,
            "cum_volume": 900_000 + i * 60_000, "prev_close": 1.00,
            "high_price": 1.31,
        }, "regular")
        if out is not None:
            m = out
    assert m is not None, "the scorer produced no metrics"
    return m


# -- the seam /check walks --------------------------------------------------

def test_the_wiring_reads_only_fields_a_real_metrics_has(cfg):
    """The regression itself: this raised AttributeError before the fix."""
    m = _real_metrics(cfg)
    assert not hasattr(m, "minutes_tracked"), "Metrics grew the field back"

    b = metrics_to_bundle(m, enr=None, cfg=cfg)

    assert b.symbol == m.symbol
    assert b.price == m.price
    assert b.minutes_tracked == pytest.approx(m.age_seconds / 60.0)
    assert b.session == m.session


def test_the_check_path_gets_a_gate_back_with_and_without_enrichment(cfg):
    """/check calls run_gate directly; it must never raise on a real Metrics."""
    m = _real_metrics(cfg)
    enr = Enrichment(symbol=m.symbol)
    enr.news = [{"title": "Acme Corp awarded $12M contract", "source": "Benzinga"}]

    for enrichment in (None, enr):
        gate, bundle = run_gate(m, enr=enrichment, cfg=cfg, now=to_et())
        assert gate is not None
        assert bundle.symbol == m.symbol
        # A verdict either way is fine; an exception is not.
        assert gate.blocked_by is None or isinstance(gate.blocked_by, str)


def test_a_quote_with_no_bid_and_no_ask_still_evaluates(cfg):
    """No configured feed publishes a book, so this is the live shape."""
    m = _bare_metrics(cfg)

    bundle = metrics_to_bundle(m, enr=None, cfg=cfg)
    gate, _ = run_gate(m, enr=None, cfg=cfg, now=to_et())

    assert bundle.bid is None and bundle.ask is None
    assert gate is not None


def test_minutes_tracked_is_derived_not_invented(cfg):
    """Where the input is missing the field stays 0.0 rather than a guess."""
    m = _real_metrics(cfg)
    del m.age_seconds  # a stand-in that has never been aged

    bundle = metrics_to_bundle(m, enr=None, cfg=cfg)

    assert bundle.minutes_tracked == 0.0


# -- the rejection record ---------------------------------------------------

def test_a_rejection_record_is_a_capped_list_of_strings(cfg, tmp_path):
    """The record was written with an unbound name and raised on first use."""
    engine = _engine(cfg, tmp_path)
    m = _real_metrics(cfg)
    gate, _ = run_gate(m, enr=None, cfg=cfg, now=to_et())

    engine._note_gate_reject(m, gate)

    row = engine._gate_rejects[m.symbol]
    assert isinstance(row["failed_on"], list)
    assert all(isinstance(x, str) for x in row["failed_on"]), row["failed_on"]
    assert len(row["failed_on"]) <= 4
    assert row["blocked_by"] == gate.blocked_by


# -- when the gate may stop an alert ----------------------------------------

def test_an_unevaluable_gate_fails_closed(cfg, tmp_path):
    """A gate that could not run must not be read as a gate that passed."""
    cfg.set("STRATEGY_GATE", "1")
    engine = _engine(cfg, tmp_path)
    m = _real_metrics(cfg)
    stats = {}

    assert engine._gate_vetoes(m, None, stats) is True
    assert stats == {"gate_errors": 1}
    assert engine._gate_rejects == {}


def test_the_advisory_gate_never_vetoes(cfg, tmp_path):
    """STRATEGY_GATE=0 keeps the tier path in charge; the gate is a report."""
    engine = _engine(cfg, tmp_path)
    assert engine._gate_is_mandatory() is False

    assert engine._gate_vetoes(_real_metrics(cfg),
                               StrategyGate(passed=False, blocked_by="ILLIQUID"),
                               {}) is False
    assert engine._gate_rejects == {}


@pytest.mark.parametrize("verdict", ["ILLIQUID", "WIDE_SPREAD", "DILUTION",
                                     "EXHAUSTED", "DATA_CONFLICT"])
def test_a_verdict_the_poll_can_reach_vetoes_and_is_recorded(cfg, tmp_path,
                                                             verdict):
    """The reality blockers come off price and volume alone, so they count now."""
    cfg.set("STRATEGY_GATE", "1")
    engine = _engine(cfg, tmp_path)
    m = _real_metrics(cfg)

    assert engine._gate_vetoes(m, StrategyGate(passed=False, blocked_by=verdict),
                               {}) is True
    assert m.symbol in engine._gate_rejects


@pytest.mark.parametrize("verdict", ["no_strategy", "no_catalyst"])
def test_a_verdict_the_poll_cannot_reach_does_not_veto(cfg, tmp_path, verdict):
    """No candles and no news at this stage: a missing input, not a decision.

    Enforcing these would reject every candidate before `_fire_alert` could
    enrich it, so switching the gate on would mute the system outright.
    """
    cfg.set("STRATEGY_GATE", "1")
    engine = _engine(cfg, tmp_path)
    m = _real_metrics(cfg)

    assert engine._gate_vetoes(m,
                               StrategyGate(passed=False, blocked_by=verdict),
                               {}) is False
    assert engine._gate_rejects == {}


def test_a_passing_gate_never_vetoes(cfg, tmp_path):
    cfg.set("STRATEGY_GATE", "1")
    engine = _engine(cfg, tmp_path)

    assert engine._gate_vetoes(_real_metrics(cfg),
                               StrategyGate(passed=True, primary="bull_flag"),
                               {}) is False


# -- what still filters while the gate is partial ---------------------------

def test_the_tier_bar_still_filters_when_the_gate_is_mandatory(cfg, tmp_path):
    """With the gate on, waiving the tier bar left the alert path unfiltered."""
    cfg.set("STRATEGY_GATE", "1")
    engine = _engine(cfg, tmp_path)

    quiet = Metrics(symbol="QUIET", ts=time.time(), price=1.10, session="regular")
    quiet.tier = TIER_NONE
    loud = Metrics(symbol="LOUD", ts=time.time(), price=1.10, session="regular")
    loud.tier = TIER_CONFIRMED

    assert engine._reached_alert_bar(quiet, "regular", fast=False) is False
    assert engine._reached_alert_bar(loud, "regular", fast=False) is True


def test_the_tier_bar_unaffected_by_the_advisory_gate(cfg, tmp_path):
    """The flag must not change who reaches the gate when it is only a report."""
    engine = _engine(cfg, tmp_path)
    quiet = Metrics(symbol="QUIET", ts=time.time(), price=1.10, session="regular")
    quiet.tier = TIER_NONE

    assert engine._reached_alert_bar(quiet, "regular", fast=False) is False


def test_status_says_when_the_gate_dropped_an_alert(cfg, tmp_path):
    """A fail-closed gate that never speaks looks exactly like a quiet morning."""
    engine = _engine(cfg, tmp_path)
    engine._last_cycle = {"alerts": 0, "ms": 12, "quotes": 40, "symbols": 7,
                          "gate_errors": 2}

    assert "strategy gate" in engine.status_text()
    assert "gate errors 2" in engine.status_text()

    engine._last_cycle = {"alerts": 1, "ms": 12, "quotes": 40, "symbols": 7}
    assert "gate errors" not in engine.status_text()


# -- /check end to end --------------------------------------------------------

class _FakeFinviz:
    """The quote feed /check fetches from; the candles and news stay empty."""

    def snapshot(self, symbols, session=None):
        rows = {s.upper(): {"symbol": s.upper(), "price": 1.23,
                            "cum_volume": 5000.0, "prev_close": 1.00,
                            "avg_volume": 100000.0, "float_shares": 18_500_000,
                            "market_cap": 26_000_000, "ts": time.time(),
                            "session": session}
                for s in symbols}
        return [rows[str(s).upper()] for s in symbols]

    def normalize_row(self, row, session):
        return None


def test_a_full_check_replies_with_the_analysis_not_an_error(cfg, tmp_path):
    """`Check failed: 'Metrics' object has no attribute 'minutes_tracked'` is
    what a reader saw here: /check runs the gate directly, so it was the first
    place the broken wiring surfaced. This walks the whole command."""
    from penny.commands import CommandHandler
    from penny.engine import Engine
    from penny.store import open_state
    from penny.telegram import FakeTelegram

    db = open_state(tmp_path / "state.db")
    engine = Engine(cfg, db, None, None, finviz=_FakeFinviz())
    telegram = FakeTelegram(cfg)
    handler = CommandHandler(cfg, db=db, journal=None, telegram=telegram,
                             engine=engine)
    cfg.set("TELEGRAM_CHAT_ID", "1")

    handler._run_check("ABCD")

    sent = "\n".join(telegram.outbox)
    assert sent, "no reply at all"
    assert "Check failed" not in sent, sent
    assert "ABCD" in sent
    # The gate block the bug erased from the report.
    assert "STRATEGY" in sent
