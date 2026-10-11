"""The pre-market gate: alert at +10% when volume backs it, not at +20%."""

from __future__ import annotations

import time

from penny.scoring import Metrics, TIER_EARLY, TIER_NONE
from penny.store import open_journal, open_state
from penny.telegram import FakeTelegram


def _engine(cfg, tmp_path):
    from penny.engine import Engine
    db = open_state(tmp_path / "state.db")
    journal = open_journal(tmp_path / "journal.db")
    telegram = FakeTelegram(cfg)
    engine = Engine(cfg, db, journal, telegram)
    # `warm` is derived from the cycle count, so raise it rather than assign it.
    engine.scorer.cycles = engine.scorer.warmup_cycles + 1
    assert engine.scorer.warm
    return engine, telegram, db, journal


def _metrics(pct, *, volx=5.0, dollar_vol=20_000.0, tier=TIER_NONE):
    m = Metrics(symbol="ABCD", ts=time.time(), price=1.10, cum_volume=500_000)
    m.prev_close = 1.10 / (1.0 + pct)
    m.pct_vs_close = pct
    m.rise = {15: pct, 60: pct}
    m.volx = {15: volx}
    m.dollar_vol = {15: dollar_vol}
    m.tier = tier
    m.score = 50
    return m


def test_a_ten_percent_premarket_move_with_volume_alerts(cfg, tmp_path):
    engine, _tg, db, journal = _engine(cfg, tmp_path)
    assert engine._premarket_breakout(_metrics(0.11), "pre") is True
    db.close()
    journal.close()


def test_the_premarket_gate_does_not_fire_in_the_regular_session(cfg, tmp_path):
    engine, _tg, db, journal = _engine(cfg, tmp_path)
    assert engine._premarket_breakout(_metrics(0.11), "regular") is False
    db.close()
    journal.close()


def test_below_ten_percent_is_not_a_premarket_alert(cfg, tmp_path):
    engine, _tg, db, journal = _engine(cfg, tmp_path)
    assert engine._premarket_breakout(_metrics(0.09), "pre") is False
    db.close()
    journal.close()


def test_a_move_with_no_volume_behind_it_does_not_alert(cfg, tmp_path):
    """A headline percentage on no trade is a spread, not a setup."""
    engine, _tg, db, journal = _engine(cfg, tmp_path)
    assert engine._premarket_breakout(_metrics(0.30, volx=0.4), "pre") is False
    assert engine._premarket_breakout(_metrics(0.30, dollar_vol=10.0), "pre") is False
    db.close()
    journal.close()


def test_a_low_tier_stock_below_the_gate_does_not_alert_at_all(cfg, tmp_path):
    engine, _tg, db, journal = _engine(cfg, tmp_path)
    assert engine._alert_candidate(_metrics(0.05, tier=TIER_NONE), "pre") is False
    db.close()
    journal.close()


def test_an_established_tier_stock_alerts_normally_before_ten_percent(cfg, tmp_path):
    engine, _tg, db, journal = _engine(cfg, tmp_path)
    assert engine._alert_candidate(_metrics(0.05, tier=TIER_EARLY), "pre") is True
    db.close()
    journal.close()
