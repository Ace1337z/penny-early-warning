"""Alerts must fire on a 3-5 minute burst, not wait for the 15/60-minute gates.

`tier_for` needs a 15-minute rise and a 10x 15-minute volume multiple before it
reaches tier 2, so by the time an alert went out the burst was over and the
reader was looking at the give-back. These pin the fast lane that replaced that
wait, and pin the guards that stop it from becoming a noise machine.
"""

from __future__ import annotations

import time

from penny.alerts import format_alert1, format_alert2
from penny.scoring import Metrics, TIER_NONE, WINDOWS


def _engine(cfg, tmp_path):
    from penny.engine import Engine
    from penny.store import open_journal, open_state
    from penny.telegram import FakeTelegram
    db = open_state(tmp_path / "state.db")
    journal = open_journal(tmp_path / "journal.db")
    engine = Engine(cfg, db, journal, FakeTelegram(cfg))
    engine.scorer.cycles = engine.scorer.warmup_cycles + 1
    return engine, db, journal


def _m(**over):
    """A tier-0 stock: nothing has filled a 15-minute window yet."""
    m = Metrics(symbol="ABCD", ts=time.time(), price=1.12, cum_volume=2_000_000)
    m.prev_close = 1.00
    m.pct_vs_close = 0.12
    m.rise = {3: 0.021, 5: 0.031, 15: 0.031, 60: 0.09, 240: 0.09}
    m.volx = {3: 6.0, 5: 6.5, 15: 0.9, 60: 0.2, 240: 0.1}
    m.dollar_vol = {3: 9000.0, 5: 14_000.0, 15: 21_000.0, 60: 5000.0, 240: 2000.0}
    m.vwap = 1.05
    m.from_high = -0.02
    m.accel = 2.0
    m.tier = TIER_NONE
    m.score = 61.0
    for k, v in over.items():
        setattr(m, k, v)
    return m


def _enr():
    from penny.enrich import Enrichment
    e = Enrichment(symbol="ABCD")
    e.news = []
    e.filings = []
    return e

# --- the window is recorded at all ------------------------------------------

def test_a_three_minute_window_is_recorded():
    """3m has to be a real window; the fast gate reads m.rise[3]."""
    assert 3 in WINDOWS
    assert 5 in WINDOWS


# --- the fast lane fires ----------------------------------------------------

def test_a_three_minute_burst_alerts(cfg, tmp_path):
    engine, db, journal = _engine(cfg, tmp_path)
    assert engine._fast_burst(_m(), "regular") is True
    db.close()
    journal.close()


def test_a_five_minute_burst_alerts(cfg, tmp_path):
    m = _m()
    m.rise[3] = 0.005                       # too small for the 3m gate
    assert engine_fires(cfg, tmp_path, m) is True


def engine_fires(cfg, tmp_path, m):
    engine, db, journal = _engine(cfg, tmp_path)
    try:
        return engine._fast_burst(m, "regular")
    finally:
        db.close()
        journal.close()


def test_the_fast_lane_lets_a_tier_zero_stock_alert(cfg, tmp_path):
    engine, db, journal = _engine(cfg, tmp_path)
    m = _m()
    assert m.tier == TIER_NONE
    assert engine._alert_candidate(m, "regular") is True
    db.close()
    journal.close()


def test_the_tier_path_alone_would_have_missed_it(cfg, tmp_path):
    """The whole point: without the fast lane this stock never alerts."""
    engine, db, journal = _engine(cfg, tmp_path)
    m = _m()
    cfg.set("FAST_ON", 0)                   # fast lane off
    assert engine._fast_burst(m, "regular") is False
    assert engine._alert_candidate(m, "regular") is False
    db.close()
    journal.close()


def test_the_fast_lane_can_be_switched_off(cfg, tmp_path):
    engine, db, journal = _engine(cfg, tmp_path)
    cfg.set("FAST_ON", 0)
    assert engine._fast_burst(_m(), "regular") is False
    db.close()
    journal.close()


# --- the guards -------------------------------------------------------------

def test_a_burst_without_volume_does_not_alert(cfg, tmp_path):
    m = _m()
    m.volx[3] = 1.0
    assert engine_fires(cfg, tmp_path, m) is False


def test_a_burst_on_no_dollar_volume_does_not_alert(cfg, tmp_path):
    m = _m()
    m.dollar_vol[3] = 100.0
    assert engine_fires(cfg, tmp_path, m) is False


def test_a_burst_that_already_gave_the_move_back_does_not_alert(cfg, tmp_path):
    """The move must be live, not a stock parked 12% under its high."""
    m = _m()
    m.from_high = -0.12
    assert engine_fires(cfg, tmp_path, m) is False


def test_a_flat_hour_does_not_veto_a_live_burst(cfg, tmp_path):
    """The 60-minute gate is exactly the delay being removed.

    A stock flat all day that suddenly prints 3% in three minutes is the penny
    mover this tool exists for. Requiring a rising hour would reject it.
    """
    m = _m()
    m.rise[60] = 0.005
    m.rise[15] = 0.005
    assert engine_fires(cfg, tmp_path, m) is True


def test_a_five_minute_only_burst_is_not_judged_on_its_empty_3m_volume(cfg, tmp_path):
    """Volume is checked on the window that triggered, not on both."""
    m = _m()
    m.rise[3] = 0.004                        # below FAST_RISE3, above FAST_RISE5
    m.volx[3] = 0.0                          # nothing in 3 minutes
    assert engine_fires(cfg, tmp_path, m) is True


def test_a_burst_under_vwap_does_not_alert(cfg, tmp_path):
    m = _m()
    m.vwap = 1.20                       # price 1.12 is below VWAP
    assert engine_fires(cfg, tmp_path, m) is False


def test_a_thin_float_does_not_alert(cfg, tmp_path):
    m = _m()
    m.cum_volume = 1_000                # under FAST_MIN_VOLUME
    assert engine_fires(cfg, tmp_path, m) is False


def test_a_down_move_does_not_alert(cfg, tmp_path):
    m = _m()
    m.pct_vs_close = -0.10
    assert engine_fires(cfg, tmp_path, m) is False


# --- the alert says so ------------------------------------------------------

def test_alert_1_leads_with_the_burst_when_fast():
    text = format_alert1(_m(), fast=True)
    assert "3m" in text and "5m" in text
    assert "[FAST]" in text


def test_alert_1_keeps_the_normal_strip_when_not_fast():
    text = format_alert1(_m(), fast=False)
    assert "3m" in text and "5m" in text
    assert "[FAST]" not in text


def test_alert_1_still_shows_the_15_and_60_minute_moves():
    """The fast alert keeps the wider windows as context; the plain one does not.

    Spec 11.1 takes 15m and 60m out of the *alert* header, because they are what
    made a live burst read like a move that was already over. The fast lane
    prints them anyway, where the burst is the reason for the message."""
    text = format_alert1(_m(), fast=True)
    assert "15m" in text and "60m" in text


def test_a_plain_alert_header_has_no_15_or_60_minute_windows():
    """Spec 11.1: the alert path reads 3m and 5m only.

    Feature 1 adds a liquidity line ("book n/a | 15m $21,000") that contains
    the string "15m" as a dollar-volume label, not as a percent window. The
    old 15m/60m *rise* windows are what the spec bans, so this test checks for
    those specifically rather than banning the literal substring entirely.
    """
    text = format_alert1(_m(), fast=False)
    rise_lines = [l for l in text.splitlines() if "%" in l]
    # The header rise line is the one with the percent format "+12.0%"
    header_rise = next(l for l in rise_lines if "vs close" in l)
    assert "15m" not in header_rise and "60m" not in header_rise


def test_a_fast_alert_is_never_told_it_is_below_the_alert_bar():
    """The tier-0 wording came from `verdict_text`, which no longer exists.

    With the standalone VERDICT block removed, the SETUP line carries this.
    """
    fast = format_alert2(_m(), _enr(), None, fast=True)
    assert "below the alert bar" not in fast
    assert "SETUP: FAST" in fast

    slow = format_alert2(_m(), _enr(), None, fast=False)
    assert "below the alert bar" in slow
    assert "SETUP: NONE" in slow

def test_a_fast_alert_2_explains_why_now():
    text = format_alert2(_m(), _enr(), None, fast=True)
    assert "WHY NOW" in text
    assert "in 3m" in text
    assert len(text) <= 3900

# --- the wording a fast alert shows -----------------------------------------
# A fast alert previously rendered "VERDICT: WAIT - below the alert bar"
# directly beneath its own "WHY NOW" block. These pin the wording.


def test_a_fast_alert_1_is_not_headlined_none(cfg):
    m = _m()
    text = format_alert1(m, fast=True)
    assert "ALERT: NONE" not in text
    assert "ALERT: FAST" in text


def test_a_non_fast_alert_1_keeps_the_tier_name(cfg):
    assert "ALERT: NONE" in format_alert1(_m(), fast=False)


def test_a_fast_alert_2_does_not_say_below_the_alert_bar(cfg):
    text = format_alert2(_m(), _enr(), None, fast=True)
    assert "below the alert bar" not in text
    assert "SETUP: FAST" in text


def test_the_fallback_verdict_receives_the_fast_flag(cfg):
    """No AI call must not produce a misleading "WAIT" on a live fast alert."""
    text = format_alert2(_m(), _enr(), None, fast=True)
    assert "VERDICT: WAIT" not in text
    assert "AI unavailable" in text


def test_an_explicit_decision_still_wins_over_the_fallback(cfg):
    """The one AI decision is authoritative when one was actually produced."""
    text = format_alert2(_m(), _enr(), None, fast=True,
                         decision="VERDICT: WAIT - distribution at the high")
    assert "WAIT" in text
    assert "AI unavailable" not in text
