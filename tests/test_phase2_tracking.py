"""Phase 2 tracking: invalidation (F4), confidence calibration (F5) and the
post-mortem (F14).

An alert that dies must say so exactly once and keep the row it died on; a
confidence the reader is shown must sit within 15 points of what that bucket
actually delivered once it has a real sample; and every losing alert must
produce one line naming the rubric clause that should have blocked it.

The EOD message grows three sections from these: ABORTS, BY AI RATING
(claimed vs actual) and POST-MORTEMS.
"""

from __future__ import annotations

import pytest

from penny import config
from penny.context import calibration as CAL
from penny.context.postmortem import build, is_loser, short_line
from penny.tracking import Tracker, eod_report
from penny.tracking_invalidation import InvalidationState, abort_message, check
from penny.util import now_et


DAY = "2026-10-12"
TODAY = now_et().strftime("%Y-%m-%d")
TS = 1_760_000_000.0


@pytest.fixture()
def db(tmp_path):
    from penny.store import open_state
    d = open_state(tmp_path / "state.db")
    yield d
    d.close()


def _tracker(db):
    return Tracker(db, clock=lambda: TS)


def _state(**kw):
    st = InvalidationState(symbol="ABCD", day=DAY, alert_ts=TS, alert_price=1.42)
    for key, value in kw.items():
        setattr(st, key, value)
    return st


# --- Feature 4: the six invalidation triggers ---------------------------------

def test_t1_a_5m_close_below_vwap_invalidates_the_setup():
    st = _state(vwap_ref=1.40, alert_time_low=1.39)
    hit = check(st, price=1.38, now=TS, five_min_close_below_vwap=True)
    assert hit is not None
    assert hit[0] == "vwap"
    assert "VWAP" in hit[1]
    assert st.fired


def test_t1_is_checked_before_the_alert_low():
    st = _state(vwap_ref=1.40, alert_time_low=1.39)
    hit = check(st, price=1.35, now=TS, five_min_close_below_vwap=True)
    assert hit[0] == "vwap"


def test_t2_breaking_the_alert_candle_low_invalidates():
    st = _state(alert_time_low=1.40)
    hit = check(st, price=1.38, now=TS)
    assert hit is not None
    assert hit[0] == "alert_low"
    assert "below the 1.40 low at alert" in hit[1]


def test_t3_a_halt_invalidates_even_at_a_fine_price():
    st = _state()
    hit = check(st, price=1.42, now=TS, halted=True)
    assert hit is not None
    assert hit[0] == "halt"
    assert hit[1] == "trading halted"


def test_t4_one_wide_poll_is_noise_and_persistence_is_a_fact():
    st = _state()
    assert check(st, price=1.42, now=1_000.0, spread_pct=3.0) is None
    assert st.wide_since == 1_000.0
    # still inside the consecutive-minute window: not yet a condition
    assert check(st, price=1.42, now=1_100.0, spread_pct=3.0) is None
    hit = check(st, price=1.42, now=1_130.0, spread_pct=3.0)
    assert hit is not None
    assert hit[0] == "spread"
    assert "2 minutes" in hit[1]


def test_t4_a_spread_that_recovers_resets_the_window():
    st = _state()
    assert check(st, price=1.42, now=1_000.0, spread_pct=3.0) is None
    assert check(st, price=1.42, now=1_060.0, spread_pct=1.0) is None
    assert st.wide_since == 0.0
    # window restarted: two minutes must elapse again
    assert check(st, price=1.42, now=1_120.0, spread_pct=3.0) is None
    assert check(st, price=1.42, now=1_250.0, spread_pct=3.0) is not None


def test_t5_a_negative_catalyst_after_the_alert_invalidates():
    st = _state()
    hit = check(st, price=1.42, now=TS,
                negative_catalyst="priced offering below the open")
    assert hit is not None
    assert hit[0] == "catalyst"
    assert "offering" in hit[1]


def test_t6_near_the_high_on_declining_volume_is_exhaustion():
    st = _state()
    assert check(st, price=1.49, now=1.0, day_high=1.50, near_high_minutes=3,
                 five_min_volume=1_000, prior_five_min_volume=1_200) is None
    assert check(st, price=1.49, now=2.0, day_high=1.50, near_high_minutes=3,
                 five_min_volume=900, prior_five_min_volume=1_000) is None
    hit = check(st, price=1.49, now=3.0, day_high=1.50, near_high_minutes=3,
                five_min_volume=800, prior_five_min_volume=900)
    assert hit is not None
    assert hit[0] == "exhausted"
    # one rising 5m volume sample resets the streak before it qualifies
    st2 = _state()
    assert check(st2, price=1.49, now=1.0, day_high=1.50, near_high_minutes=2,
                 five_min_volume=1_000, prior_five_min_volume=1_200) is None
    assert check(st2, price=1.49, now=2.0, day_high=1.50, near_high_minutes=2,
                 five_min_volume=1_500, prior_five_min_volume=1_000) is None
    assert check(st2, price=1.49, now=3.0, day_high=1.50, near_high_minutes=2,
                 five_min_volume=1_000, prior_five_min_volume=1_500) is None


def test_only_the_first_trigger_fires_and_it_fires_once():
    st = _state(vwap_ref=1.40, alert_time_low=1.39)
    first = check(st, price=1.35, now=TS, five_min_close_below_vwap=True,
                  halted=True)
    assert first[0] == "vwap"
    assert check(st, price=1.30, now=TS + 60, halted=True) is None
    assert st.fired


def test_the_abort_message_is_an_instruction_not_an_opinion():
    text = abort_message("ABCD", 1.35, 1.42, "5m close below VWAP 1.40",
                         reason_key="vwap", ts=TS)
    assert "ABORT" in text
    assert "action: do not enter / exit if holding" in text
    assert "rubric R1 clause" in text
    assert "since alert:" in text
    # long-only: the abort never proposes a direction
    assert "short" not in text.lower()


def test_an_aborted_alert_is_terminal_and_only_ever_aborts_once(db):
    tr = _tracker(db)
    tr.start("ABCD", alert_ts=TS, alert_price=1.42, day=DAY,
             primary="low_float_squeeze")
    tr.update("ABCD", price=1.38, high=1.44, low=1.37, day=DAY)
    tr.abort("ABCD", reason="5m close below VWAP 1.29", ts=TS, day=DAY)
    tr.abort("ABCD", reason="something else", ts=TS + 60, day=DAY)

    row = tr.rows_for(DAY)[0]
    assert row["status"] == "aborted"
    assert row["aborted"] == 1
    assert row["abort_reason"] == "5m close below VWAP 1.29"
    assert row["abort_ts"] == TS

    # close_day never rescues an aborted row back into the graded set
    tr.close_day(DAY)
    assert tr.rows_for(DAY)[0]["status"] == "aborted"


# --- Feature 5: confidence calibration ---------------------------------------

def _closed(hit_t1=0, *, rating="ENTER", primary="low_float_squeeze",
            day=TODAY):
    return {"day": day, "status": "closed", "ai_rating": rating,
            "primary_strategy": primary, "hit_t1": hit_t1, "hit_stop": 0,
            "close_pct": 0.05 if hit_t1 else -0.03, "max_gain_pct": 0.08}


def test_thirty_closed_alerts_make_a_prior_and_twenty_nine_do_not():
    rows = [_closed(i % 2) for i in range(30)]
    table = CAL.build_table(rows, window_days=90, min_samples=30)
    key = CAL.bucket_key("ENTER", "low_float_squeeze")
    assert table[key]["sufficient"]
    assert table[key]["alerts"] == 30
    assert table[key]["t1_hit_rate"] == pytest.approx(0.5)

    small = CAL.build_table(rows[:29], window_days=90, min_samples=30)
    assert not small[key]["sufficient"]
    assert CAL.prior_for(small, "ENTER", "low_float_squeeze") is None
    assert CAL.prior_for(table, "ENTER", "low_float_squeeze") is not None


def test_open_and_aborted_alerts_never_enter_the_prior():
    rows = [{"day": TODAY, "status": "open", "ai_rating": "ENTER",
             "primary_strategy": "low_float_squeeze", "hit_t1": 1},
            {"day": TODAY, "status": "aborted", "ai_rating": "ENTER",
             "primary_strategy": "low_float_squeeze", "hit_t1": 0}]
    assert CAL.build_table(rows, window_days=90, min_samples=30) == {}


def test_rows_outside_the_window_fall_out_of_the_prior():
    stale = [_closed(1, day="2020-01-01")] * 40
    assert CAL.build_table(stale, window_days=90, min_samples=30) == {}


def test_the_clamp_keeps_confidence_within_15_points_of_realised_hit_rate():
    prior = {"alerts": 40, "sufficient": True, "t1_hit_rate": 0.5}
    value, flag = CAL.clamp_confidence(0.90, prior, margin=0.15)
    assert value == pytest.approx(0.65)
    assert flag == "confidence_clamped_to_history"
    # already inside the ceiling: left alone, unflagged
    value, flag = CAL.clamp_confidence(0.60, prior)
    assert value == 0.60 and flag == ""
    # no prior, no clamp: the raw number is the honest one
    value, flag = CAL.clamp_confidence(0.95, None)
    assert value == 0.95 and flag == ""


def test_the_reader_sees_the_calibrated_confidence_on_the_row(db):
    tr = _tracker(db)
    tr.start("ABCD", alert_ts=TS, alert_price=1.42, day=DAY,
             primary="low_float_squeeze")
    tr.write_confidence("ABCD", 0.65, day=DAY)
    assert tr.rows_for(DAY)[0]["ai_confidence_calibrated"] == pytest.approx(0.65)
    # an unknown confidence never blanks a known one
    tr.write_confidence("ABCD", None, day=DAY)
    assert tr.rows_for(DAY)[0]["ai_confidence_calibrated"] == pytest.approx(0.65)


def test_the_prior_block_tells_the_ai_the_sample_size():
    prior = {"alerts": 40, "sufficient": True, "t1_hit_rate": 0.5,
             "stop_hit_rate": 0.2, "median_close_pct": 0.03}
    text = CAL.prior_lines(prior, label="ENTER")
    assert "CALIBRATION PRIOR (ENTER)" in text
    assert "alerts: 40" in text
    assert "T1 hit: 50%" in text
    assert CAL.prior_lines(None) == ""


# --- the AI rating lands on the row ------------------------------------------

def test_the_ai_rating_and_findings_land_on_the_row(db):
    tr = _tracker(db)
    tr.start("ABCD", alert_ts=TS, alert_price=1.42, day=DAY)
    tr.write_decision("ABCD", rating="STRONG", verdict="ENTER",
                      findings=["held the reclaim", "float turning over"],
                      day=DAY)
    row = tr.rows_for(DAY)[0]
    assert row["ai_rating"] == "STRONG"
    assert row["ai_verdict"] == "ENTER"
    assert "held the reclaim" in row["ai_key_findings"]

    # nothing known yet must not blank what is known
    tr.write_decision("ABCD", day=DAY)
    row = tr.rows_for(DAY)[0]
    assert row["ai_rating"] == "STRONG"
    assert row["ai_verdict"] == "ENTER"


# --- Feature 14: the post-mortem ---------------------------------------------

def test_a_winner_has_no_postmortem_and_a_loser_always_does():
    winner = {"status": "closed", "close_pct": 0.10, "symbol": "WIN"}
    assert not is_loser(winner)
    assert build(winner) is None

    loser = {"status": "closed", "close_pct": -0.05, "symbol": "LOSR",
             "primary_strategy": "gap_and_go", "max_gain_pct": 0.01,
             "ai_rating": "MODERATE", "ai_verdict": "ENTER"}
    assert is_loser(loser)
    text = build(loser)
    assert "Suggest:" in text
    assert "R1 momentum clause should have blocked" in text
    assert "the AI rated it MODERATE / ENTER" in text


def test_an_aborted_alert_is_always_a_loser():
    assert is_loser({"status": "aborted"})
    assert is_loser({"status": "closed", "close_pct": 0.05, "hit_stop": 1})
    assert not is_loser({"status": "closed", "close_pct": 0.0,
                         "hit_stop": 0, "hit_t1": 1})


def test_the_postmortem_names_the_clause_for_each_kind_of_failure():
    halt = {"status": "aborted", "symbol": "H", "abort_reason": "trading halted",
            "primary_strategy": "gap_and_go"}
    assert "R3 halt clause should have blocked" in build(halt)

    spread = {"status": "aborted", "symbol": "S",
              "abort_reason": "spread 3.00% for 2 minutes",
              "primary_strategy": "gap_and_go"}
    assert "R2 spread clause should have blocked" in build(spread)

    dilution = {"status": "aborted", "symbol": "D",
                "abort_reason": "priced offering below the open",
                "primary_strategy": "gap_and_go"}
    assert "R4 negative-catalyst clause should have blocked" in build(dilution)

    # No clause matches: say so rather than invent one.
    unknown = {"status": "closed", "close_pct": -0.05, "max_gain_pct": 0.50,
               "symbol": "U", "primary_strategy": "gap_and_go"}
    assert "Nothing in the rubric covered this" in build(unknown)


def test_the_eod_postmortem_line_is_one_line_per_loser():
    line = short_line({"status": "aborted", "symbol": "ABCD",
                       "primary_strategy": "low_float_squeeze",
                       "abort_reason": "5m close below VWAP"})
    assert line.startswith("ABCD  low_float_squeeze;")
    assert "Suggest:" in line
    assert "\n" not in line


# --- the EOD sections together -----------------------------------------------

def _day_with_a_win_a_loss_and_an_abort(tr):
    tr.start("ABCD", alert_ts=TS, alert_price=1.42, day=DAY,
             primary="low_float_squeeze", plan={"stop": 1.30, "t1": 1.60})
    tr.update("ABCD", price=1.38, high=1.44, low=1.37, day=DAY)
    tr.abort("ABCD", reason="5m close below VWAP 1.29", ts=TS, day=DAY)

    tr.start("IJKL", alert_ts=TS, alert_price=0.50, day=DAY,
             primary="gap_and_go", plan={"stop": 0.45, "t1": 0.55})
    tr.update("IJKL", price=0.55, high=0.56, low=0.49, day=DAY)

    tr.start("MNOP", alert_ts=TS, alert_price=0.80, day=DAY,
             primary="first_green_day", plan={"stop": 0.75, "t1": 0.96})
    tr.update("MNOP", price=0.72, high=0.81, low=0.71, day=DAY)

    tr.write_decision("IJKL", rating="STRONG", verdict="ENTER",
                      findings=["held the reclaim"], day=DAY)
    tr.write_confidence("IJKL", 0.71, day=DAY)
    tr.write_decision("MNOP", rating="MODERATE", verdict="PASS",
                      findings=["second daily red"], day=DAY)
    tr.write_confidence("MNOP", 0.55, day=DAY)
    tr.close_day(DAY)


def test_the_eod_report_has_aborts_ratings_and_postmortems(db):
    tr = _tracker(db)
    _day_with_a_win_a_loss_and_an_abort(tr)
    report = eod_report(tr, DAY)

    assert "alerts 3 | closed 2 | aborted 1 | invalidate 0" in report

    # ABORTS: time, entry, where it went anyway, and the reason
    assert "ABORTS (1)" in report
    abort_line = report.split("ABORTS (1)")[1].split("\n")[1]
    assert "entry $1.42" in abort_line
    assert "now $1.38" in abort_line
    assert "5m close below VWAP" in abort_line

    # BY AI RATING: what was claimed next to what the tape delivered
    assert "BY AI RATING" in report
    rating_block = report.split("BY AI RATING")[1].split("\n\n")[0]
    assert "STRONG" in rating_block
    assert "claimed 0.71 vs actual 1.00" in rating_block
    assert "MODERATE" in rating_block
    assert "claimed 0.55 vs actual 0.00" in rating_block

    # POST-MORTEMS: one line per loser, and only the losers
    assert "POST-MORTEMS (2)" in report
    pm_block = report.split("POST-MORTEMS (2)")[1]
    assert "ABCD" in pm_block
    assert "MNOP" in pm_block
    assert "IJKL" not in pm_block


def test_postmortems_can_be_switched_out_of_the_eod(db, monkeypatch):
    tr = _tracker(db)
    _day_with_a_win_a_loss_and_an_abort(tr)
    monkeypatch.setitem(config.DEFAULTS, "POSTMORTEM_IN_EOD", "0")
    assert "POST-MORTEMS" not in eod_report(tr, DAY)
    monkeypatch.setitem(config.DEFAULTS, "POSTMORTEM_IN_EOD", "1")
    assert "POST-MORTEMS (2)" in eod_report(tr, DAY)
