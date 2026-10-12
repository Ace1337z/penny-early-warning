"""Phase 2 context: analogs (F6), the multi-timeframe block (F8), the sector
heatmap (F9) and catalyst decay (F15).

Every block here is context for a human reader or for the one AI call -- none
of it can create an alert on its own. What they must never do is invent:
a hit rate from three trades is suppressed, a missing daily history reads
"unknown", a cold sector gets no line at all, and a negative catalyst does not
get less bad because time passed.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from penny.config import DEFAULTS
from penny.context.analogs import analog_line, features_of, find_neighbors, summarize
from penny.context.multi_timeframe import block_lines, build_context, daily_ma
from penny.context.sector_heatmap import SectorHeatmap, sector_line
from penny.store import open_state
from penny.strategies.base import Bundle
from penny.strategies.news_cause import (DILUTION, HALF_LIFE_MIN, HARD, MEDIUM,
                                         SOFT, WEIGHTS, classify, effective_weight,
                                         rank_catalysts)
from penny.tracking import Tracker


DAY = "2026-10-12"
TS = 1_760_000_000.0


@pytest.fixture()
def db(tmp_path):
    d = open_state(tmp_path / "state.db")
    yield d
    d.close()


def _tracker(db):
    return Tracker(db, clock=lambda: TS)


# --- Feature 6: historical analogs -------------------------------------------

def _analog_row(i, **kw):
    row = {"symbol": "S%02d" % i, "float_shares": 1_000_000 + i * 50_000,
           "pct_vs_close": 0.05 + i * 0.01, "volx": 3.0 + i * 0.4,
           "gap_pct": 0.02, "net5d": 0.1, "catalyst_class": "HARD",
           "minutes_since_open": 60 + i * 5, "regime": 1.0,
           "hit_t1": 1 if i % 3 == 0 else 0, "hit_stop": 1 if i % 4 == 0 else 0,
           "max_gain_pct": 0.10 + i * 0.01, "peak_minutes": 30 + i}
    row.update(kw)
    return row


def test_a_row_becomes_an_eight_feature_vector():
    vec = features_of(_analog_row(1))
    assert len(vec) == 8
    assert all(isinstance(v, float) for v in vec)
    # a row whose float cannot be read is unusable, not zero-filled
    assert features_of({"float_shares": "not a number"}) is None


def test_the_nearest_neighbor_of_an_alert_is_itself():
    pool = [_analog_row(i) for i in range(14)]
    neighbors, distances = find_neighbors(features_of(pool[6]), pool, k=5, min_k=3)
    assert neighbors[0] is pool[6]
    assert distances[0] == pytest.approx(0.0, abs=1e-9)
    assert len(neighbors) == 5


def test_fewer_than_min_k_neighbors_suppresses_the_summary():
    pool = [_analog_row(i) for i in range(5)]
    neighbors, _ = find_neighbors(features_of(pool[0]), pool, k=20, min_k=8)
    assert len(neighbors) == 5
    assert summarize(neighbors, min_k=8) is None
    assert analog_line(None) == ""


def test_the_analog_line_reports_rates_and_the_median_peak():
    pool = [_analog_row(i) for i in range(10)]
    summary = summarize(pool, min_k=8)
    assert summary["n"] == 10
    assert summary["t1_hit_rate"] == pytest.approx(0.4)
    assert summary["stop_hit_rate"] == pytest.approx(0.3)
    line = analog_line(summary)
    assert line.startswith("ANALOGS: 10 similar setups")
    assert "T1 40%" in line
    assert "stop 30%" in line
    assert "median peak" in line and "@ " in line


def test_write_context_maps_the_analog_summary_onto_the_row(db):
    tr = _tracker(db)
    tr.start("ABCD", alert_ts=TS, alert_price=1.42, day=DAY)
    tr.write_context("ABCD", day=DAY,
                     analogs={"t1_hit_rate": 0.4, "stop_hit_rate": 0.3,
                              "median_peak_pct": 0.145})
    row = tr.rows_for(DAY)[0]
    assert row["analog_t1_hit_rate"] == pytest.approx(0.4)
    assert row["analog_stop_hit_rate"] == pytest.approx(0.3)
    assert row["analog_median_peak_pct"] == pytest.approx(0.145)


def test_write_context_never_blanks_a_richer_earlier_write(db):
    tr = _tracker(db)
    tr.start("ABCD", alert_ts=TS, alert_price=1.42, day=DAY)
    tr.write_context("ABCD", day=DAY, bid=1.41, ask=1.43, spread_pct=1.41)
    tr.write_context("ABCD", day=DAY, dollar_vol_15m=1_100_000,
                     sector="Biotech", sector_hot=True, exit_liq_2k_seconds=60)
    row = tr.rows_for(DAY)[0]
    assert row["bid_at_alert"] == pytest.approx(1.41)
    assert row["ask_at_alert"] == pytest.approx(1.43)
    assert row["dollar_vol_15m_at_alert"] == pytest.approx(1_100_000)
    assert row["sector_at_alert"] == "Biotech"
    assert row["sector_hot"] == 1
    assert row["exit_liq_2k_seconds"] == pytest.approx(60)

    # a later call that only knows the sector leaves the book columns alone
    tr.write_context("ABCD", day=DAY, sector="Tech")
    row = tr.rows_for(DAY)[0]
    assert row["bid_at_alert"] == pytest.approx(1.41)
    assert row["sector_at_alert"] == "Tech"


# --- Feature 8: multi-timeframe context, AI-only -----------------------------

def test_the_mtf_block_is_marked_ai_only():
    text = block_lines(build_context())
    assert text.startswith(
        "MULTI-TIMEFRAME CONTEXT (for reasoning only; not shown to the user)")
    assert "daily trend:" in text
    assert "60m trend:" in text


def test_without_daily_history_every_field_says_unknown_rather_than_guessing():
    ctx = build_context()
    assert ctx["daily_trend"] == "unknown (no daily history)"
    assert ctx["daily_range_position"] == "unknown"
    assert ctx["weekly_float_turnover"] == "unknown"
    assert ctx["prior_5d_shape"] == "unknown"
    assert ctx["trend_60m"] == "unknown"


def test_the_trend_is_read_against_the_20_day_mean():
    closes = [{"close": 1.0 + i * 0.01, "low": 0.98 + i * 0.01}
              for i in range(20)]
    ctx = build_context(daily=closes, prev_close=1.30, day_high=1.31,
                        rise_60=0.04)
    assert "above 20d MA" in ctx["daily_trend"]
    assert ctx["trend_60m"] == "+4.0%"
    # under the mean reads "below", never an invented extension
    below = build_context(daily=closes, prev_close=1.00)
    assert "below 20d MA" in below["daily_trend"]


def test_a_short_history_is_not_called_a_20_day_mean():
    assert daily_ma([1.0] * 5) is None
    assert daily_ma([1.0 + i for i in range(20)]) is not None


def test_block_lines_is_empty_when_there_is_nothing_to_say():
    assert block_lines(None) == ""
    assert block_lines({}) == ""


# --- Feature 9: the sector heatmap -------------------------------------------

def _heatmap(now=1_000.0, **kw):
    state = {"now": now}
    kw.setdefault("min_hot_pct", 2.0)
    kw.setdefault("top_n", 3)
    return SectorHeatmap(clock=lambda: state["now"], **kw), state


def test_a_hot_sector_gets_a_line_and_a_cold_sector_gets_none():
    hm, _ = _heatmap()
    hm.update([{"sector": "Biotech", "change_pct": 12.0},
               {"sector": "Biotech", "change_pct": 9.0},
               {"sector": "Biotech", "change_pct": 8.5},
               {"sector": "Gold", "change_pct": 0.2},
               {"sector": "Gold", "change_pct": -0.1}])
    line = sector_line(hm, "biotech")
    assert line.startswith("SECTOR: biotech")
    assert "(hot, 3 names on the list)" in line
    # a cold sector produces no line at all, not a "not hot" line
    assert sector_line(hm, "gold") == ""
    assert sector_line(hm, "") == ""


def test_one_extreme_name_does_not_heat_a_sector():
    hm, _ = _heatmap(top_n=5)
    hm.update([{"sector": "gold", "change_pct": 900.0},
               {"sector": "gold", "change_pct": 0.3},
               {"sector": "gold", "change_pct": -0.2}])
    stats = hm.sector_stats()["gold"]
    assert stats["median_pct"] == pytest.approx(0.3)
    assert stats["hot"] is False
    assert sector_line(hm, "gold") == ""


def test_the_heatmap_refreshes_on_the_interval_not_on_every_poll():
    hm, state = _heatmap(refresh_seconds=300)
    hm.update([{"sector": "biotech", "change_pct": 9.0}])
    state["now"] += 100
    hm.update([{"sector": "tech", "change_pct": 9.0}])
    assert hm.rows[0]["sector"] == "biotech"        # inside the window: ignored
    state["now"] += 300
    hm.update([{"sector": "tech", "change_pct": 9.0}])
    assert hm.rows[0]["sector"] == "tech"           # window elapsed: taken


def test_the_float_rank_never_crowns_the_thinnest_of_one():
    hm, _ = _heatmap()
    assert hm.float_rank("biotech", 1_000_000, None) is None
    assert hm.float_rank("biotech", None, {"A": 2_000_000}) is None
    assert hm.float_rank("", 1_000_000, {"A": 2_000_000}) is None
    assert hm.float_rank("biotech", 1_500_000,
                         {"A": 500_000, "B": 1_000_000}) == 3


def test_the_sector_line_names_the_float_rank_only_when_it_is_not_first():
    hm, _ = _heatmap()
    hm.update([{"sector": "biotech", "change_pct": 9.0},
               {"sector": "biotech", "change_pct": 8.0},
               {"sector": "biotech", "change_pct": 7.0}])
    line = sector_line(hm, "biotech", float_shares=1_500_000,
                       sector_floats={"A": 500_000, "B": 1_000_000})
    assert "| 3 thinnest float in the sector" in line
    first = sector_line(hm, "biotech", float_shares=400_000,
                        sector_floats={"A": 500_000, "B": 1_000_000})
    assert "thinnest" not in first


# --- Feature 15: catalyst decay ----------------------------------------------

def test_each_catalyst_class_halves_on_its_own_schedule():
    assert effective_weight(1.0, HARD, 180) == pytest.approx(0.5)
    assert effective_weight(1.0, MEDIUM, 120) == pytest.approx(0.5)
    assert effective_weight(1.0, SOFT, 30) == pytest.approx(0.5)
    assert effective_weight(1.0, HARD, 360) == pytest.approx(0.25)


def test_a_negative_catalyst_never_decays():
    assert HALF_LIFE_MIN[DILUTION] == 0.0
    assert WEIGHTS[DILUTION] == -1.0
    assert effective_weight(-1.0, DILUTION, 10_000) == -1.0


def test_an_unknown_age_keeps_the_full_weight():
    assert effective_weight(1.0, HARD, None) == 1.0
    assert effective_weight(1.0, HARD, -5.0) == 1.0


def test_the_configured_half_lives_are_the_curves_keys():
    assert float(DEFAULTS["HALF_LIFE_HARD"]) == 180
    assert float(DEFAULTS["HALF_LIFE_MEDIUM"]) == 120
    assert float(DEFAULTS["HALF_LIFE_SOFT"]) == 30
    # a caller-supplied curve is honoured rather than ignored
    assert effective_weight(1.0, HARD, 90,
                            half_life={"HARD": 90.0}) == pytest.approx(0.5)


def test_classifying_a_headline_puts_a_dilution_first():
    assert classify("Company prices $12M public offering at $0.55") == DILUTION
    assert classify("Company receives FDA approval for its treatment") == HARD
    assert classify("Insider buys shares in the open market") == MEDIUM
    assert classify("To present at the annual investor conference") == SOFT


def test_a_stale_hard_catalyst_still_outranks_a_fresh_soft_one():
    b = Bundle(symbol="ABCD", prev_close=1.0, open_price=1.0,
               now_et=datetime(2026, 10, 12, 14, 0),
               news=[{"title": "Company receives FDA approval",
                      "source": "reuters", "age": "3h"},
                     {"title": "To present at the annual investor conference",
                      "source": "reuters", "age": "10m"}])
    best, ranked, _ = rank_catalysts(b)
    hard = next(e for e in ranked if e["class"] == HARD)
    soft = next(e for e in ranked if e["class"] == SOFT)
    # one half-life old: half its pull, and half still beats a fresh 0.25
    assert hard["weight"] == pytest.approx(0.5)
    # ten minutes old, a SOFT has already lost a fifth of its (small) pull
    assert soft["weight"] == pytest.approx(0.25 * 2 ** (-10.0 / 30.0))
    assert best is hard
