"""Per-alert tracking from the alert to the close, and the one daily report.

An ungraded alert is just a feeling. These pin the three things that make the
grading honest: tracking runs off polled quotes (no extra data spend), a missing
close is `invalid` rather than a fake flat day, and the day is reported once.
"""

from __future__ import annotations

import pytest

from penny.tracking import Tracker, eod_report, stats_report, summary_line


DAY = "2026-10-09"
TS = 1_760_000_000.0


@pytest.fixture()
def db(tmp_path):
    from penny.store import open_state
    d = open_state(tmp_path / "state.db")
    yield d
    d.close()


def _tracker(db):
    return Tracker(db, clock=lambda: TS)


def _alert(tr, symbol, price, **kw):
    kw.setdefault("primary", "low_float_squeeze")
    return tr.start(symbol, alert_ts=TS, alert_price=price, day=DAY, **kw)


def _tick(tr, symbol, price, high, low):
    tr.update(symbol, price=price, high=high, low=low, day=DAY)


def _run_one(tr, symbol, alert_price, ticks, plan=None):
    _alert(tr, symbol, alert_price, plan=plan)
    for price, high, low in ticks:
        _tick(tr, symbol, price, high, low)
    tr.close_day(DAY)
    rows = [r for r in tr.rows_for(DAY) if r["symbol"] == symbol]
    return rows[0]


def _run_one_row(tr, symbol):
    rows = [r for r in tr.rows_for(DAY) if r["symbol"] == symbol]
    return rows[0]


# --- lifecycle ---------------------------------------------------------------

def test_an_alert_opens_a_row(db):
    tr = _tracker(db)
    row_id = _alert(tr, "GRND", 1.05, gate_score=71.0)
    assert row_id is not None
    assert tr.open_symbols(DAY) == ["GRND"]


def test_a_re_alert_refreshes_the_row_instead_of_duplicating_it(db):
    tr = _tracker(db)
    _alert(tr, "GRND", 1.05, primary="low_float_squeeze")
    _alert(tr, "GRND", 1.12, primary="vwap_reclaim")
    rows = tr.rows_for(DAY)
    assert len(rows) == 1
    assert abs(rows[0]["alert_price"] - 1.12) < 1e-9
    assert rows[0]["primary_strategy"] == "vwap_reclaim"


def test_two_symbols_are_two_rows(db):
    tr = _tracker(db)
    _alert(tr, "GRND", 1.05)
    _alert(tr, "BRST", 0.42)
    assert sorted(tr.open_symbols(DAY)) == ["BRST", "GRND"]


# --- the numbers ------------------------------------------------------------

def test_a_winner_records_gain_max_and_drawdown(db):
    tr = _tracker(db)
    row = _run_one(tr, "GRND", 1.00, [(1.05, 1.06, 0.99), (1.20, 1.25, 1.04)])
    assert row["status"] == "closed"
    assert abs(row["max_gain_pct"] - 0.25) < 1e-6
    assert abs(row["max_drawdown_pct"] + 0.01) < 1e-6    # the 0.99 low, not the last print
    assert abs(row["close_pct"] - 0.20) < 1e-6
    assert row["max_gain_pct"] >= row["close_pct"]


def test_a_loser_records_a_negative_close(db):
    tr = _tracker(db)
    row = _run_one(tr, "GRND", 1.00, [(0.90, 0.95, 0.88)])
    assert row["close_pct"] < 0
    assert row["max_drawdown_pct"] <= row["close_pct"]


def test_the_session_low_counts_even_when_the_last_price_recovered(db):
    """The running low comes from the polled low, not only the last print."""
    tr = _tracker(db)
    row = _run_one(tr, "GRND", 1.00, [(1.00, 1.02, 0.80), (1.10, 1.12, 1.09)])
    assert abs(row["max_drawdown_pct"] + 0.20) < 1e-6
    assert row["close_pct"] > 0


def test_stop_and_target_hits_are_sticky(db):
    """A target hit before the close still counts after the close gave it back."""
    tr = _tracker(db)
    plan = {"stop": 0.95, "t1": 1.15, "t2": 1.30}
    row = _run_one(tr, "GRND", 1.00, [(1.20, 1.22, 1.00), (0.95, 0.96, 0.94)], plan)
    assert row["hit_t1"] == 1
    assert row["hit_stop"] == 1
    assert not row["hit_t2"]


def test_a_plan_free_alert_still_tracks(db):
    """A gate pass with no plan levels must still produce a graded row."""
    tr = _tracker(db)
    row = _run_one(tr, "GRND", 1.00, [(1.05, 1.06, 0.99)])
    assert row["status"] == "closed"
    assert not row["hit_stop"] and not row["hit_t1"]


# --- a missing close is never a fake flat day --------------------------------

def test_a_symbol_that_was_quoted_closes_normally(db):
    tr = _tracker(db)
    _alert(tr, "HALT", 1.00)
    _tick(tr, "HALT", 1.05, 1.06, 0.99)
    tr.close_day(DAY)
    assert _run_one_row(tr, "HALT")["status"] == "closed"


def test_a_symbol_that_stopped_being_quoted_is_invalid_not_flat(db):
    """The point of `invalid`: a halt must not read as a 0% day."""
    tr = _tracker(db)
    _alert(tr, "GONE", 1.00)
    tr.close_day(DAY)
    assert _run_one_row(tr, "GONE")["status"] == "invalid"


def test_close_day_is_idempotent(db):
    tr = _tracker(db)
    _alert(tr, "A", 1.00)
    _alert(tr, "B", 1.00)
    _tick(tr, "A", 1.10, 1.11, 0.99)
    assert tr.close_day(DAY) == 2
    assert tr.close_day(DAY) == 0


def test_updates_after_the_close_do_not_resurrect_a_row(db):
    tr = _tracker(db)
    _alert(tr, "GRND", 1.00)
    _tick(tr, "GRND", 1.10, 1.11, 0.99)
    tr.close_day(DAY)
    _tick(tr, "GRND", 1.50, 1.51, 1.49)
    row = tr.rows_for(DAY)[0]
    assert row["status"] == "closed"
    assert abs(row["close_pct"] - 0.10) < 1e-6


# --- the one daily message ---------------------------------------------------

def test_the_eod_report_leads_with_the_count(db):
    tr = _tracker(db)
    _run_one(tr, "GRND", 1.00, [(1.20, 1.22, 1.00)])
    _run_one(tr, "BRST", 2.00, [(1.80, 1.85, 1.78)])
    text = eod_report(tr, DAY)
    assert text.startswith(f"END OF DAY {DAY}")
    assert "alerts 2" in text
    assert "closed 2" in text
    assert "up 1 / down 1" in text


def test_the_eod_report_names_the_strategies_that_fired(db):
    tr = _tracker(db)
    _alert(tr, "GRND", 1.00, primary="vwap_reclaim")
    _tick(tr, "GRND", 1.20, 1.21, 1.00)
    tr.close_day(DAY)
    assert "vwap_reclaim" in eod_report(tr, DAY)


def test_a_day_with_no_alerts_says_so_rather_than_staying_silent(db):
    assert "no alerts" in eod_report(_tracker(db), DAY)


def test_the_eod_report_does_not_count_invalid_rows_as_flat(db):
    tr = _tracker(db)
    _alert(tr, "GONE", 1.00)
    tr.close_day(DAY)
    text = eod_report(tr, DAY)
    assert "closed 0" in text
    assert "invalidate 1" in text


def test_stats_groups_and_ranks_by_strategy(db):
    tr = _tracker(db)
    _run_one(tr, "A", 1.00, [(1.10, 1.11, 1.00)])
    _alert(tr, "B", 1.00, primary="dollar_breakout")
    _tick(tr, "B", 1.20, 1.21, 1.00)
    _alert(tr, "C", 1.00, primary="dollar_breakout")
    _tick(tr, "C", 0.90, 1.00, 0.89)
    tr.close_day(DAY)
    text = stats_report(tr, days=3650)
    assert text.startswith("STATS")
    assert "low_float_squeeze" in text
    assert "dollar_breakout" in text
    assert text.index("dollar_breakout") > text.index("low_float_squeeze")


def test_stats_with_no_history_says_so(db):
    assert "no closed alerts" in stats_report(_tracker(db))


def test_summary_line_is_one_readable_row():
    row = {"symbol": "GRND", "close_pct": 0.12, "max_gain_pct": 0.25, "max_drawdown_pct": -0.03, "primary_strategy": "vwap_reclaim", "hit_stop": 0, "hit_t1": 1, "hit_t2": 0}
    line = summary_line(row)
    assert "GRND" in line and "close" in line and "max" in line and "low" in line
    assert "[1]" in line
    assert "\n" not in line
