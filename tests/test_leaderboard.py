"""Model leaderboard, eligibility, composite formula and selection."""

from __future__ import annotations

import time

from penny.ai.catalog import ModelCatalog
from penny.ai.evaluate import BASELINE_MODELS
from penny.ai.leaderboard import (composite_from, eligibility, leaderboard, select_panel,
                                  staged_elimination, wilson_interval)


def test_composite_formula():
    assert abs(composite_from(0.8, 0.0) - 0.88) < 1e-9
    assert abs(composite_from(0.5, 0.125) - 0.5) < 1e-9
    assert abs(composite_from(0.5, 0.25) - 0.3) < 1e-9
    # MAPE above the 25% target cannot go below the hit-rate floor.
    assert abs(composite_from(0.5, 0.9) - 0.3) < 1e-9


def test_wilson_interval():
    lo, hi = wilson_interval(80, 100)
    assert 0.7 < lo < 0.8 < hi < 0.9
    assert wilson_interval(0, 0) == (0.0, 1.0)


def _insert(db, model, alert_id, hit, abs_err, ts=None, status="evaluated"):
    db.execute(
        "INSERT INTO predictions(alert_id, symbol, model, ts, session, price0, horizon, "
        "due_ts, pred_price, pred_dir, exp_peak, exp_low, active, actual_price, hit, "
        "abs_err, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (alert_id, "TEST", model, ts or time.time(), "regular", 1.0, "15m",
         (ts or time.time()) + 900, 1.1, "up", 1.2, 0.9, 1, 1.05, hit, abs_err, status))


def _insert_calls(db, model, n, ok=True):
    for _ in range(n):
        db.execute("INSERT INTO ai_calls(ts, model, kind, ok, latency_ms, tokens, cached) "
                   "VALUES(?,?,?,?,?,?,0)",
                   (time.time(), model, "alert", 1 if ok else 0, 1200, 200))


def test_eligibility_requires_data(state, cfg):
    cfg.set("MIN_EVALUATED", "10")
    ok, reason = eligibility(state, "solo", cfg)
    assert ok is False
    assert "evaluated alerts" in reason


def test_eligibility_and_leaderboard_order(state, cfg):
    cfg.set("MIN_EVALUATED", "10")
    # A strong model: 20 evaluated alerts, mostly hits, small error.
    for i in range(20):
        _insert(state, "good", alert_id=i, hit=1, abs_err=0.02)
    _insert_calls(state, "good", 20, ok=True)
    # A weak model: mostly misses, large error.
    for i in range(20):
        _insert(state, "bad", alert_id=i, hit=0, abs_err=0.5)
    _insert_calls(state, "bad", 20, ok=True)
    # Baselines, deliberately poor so "good" can beat them.
    for name, model_id in BASELINE_MODELS.items():
        for i in range(20):
            _insert(state, model_id, alert_id=i, hit=0, abs_err=0.5)

    catalog = ModelCatalog(cfg.catalog_path)
    catalog.add("good", 1.0)
    catalog.add("bad", 1.0)

    rows = {r["model"]: r for r in leaderboard(state, cfg, catalog)}
    assert rows["good"]["composite"] > rows["bad"]["composite"]
    assert rows["good"]["eligible"] is True
    assert rows["bad"]["eligible"] is False


def test_select_panel_returns_at_most_three(state, cfg):
    cfg.set("MIN_EVALUATED", "5")
    catalog = ModelCatalog(cfg.catalog_path)
    for name in ("a1", "a2", "a3", "a4"):
        catalog.add(name, 1.0)
        for i in range(8):
            _insert(state, name, alert_id=i, hit=1, abs_err=0.02)
        _insert_calls(state, name, 8, ok=True)
    for model_id in BASELINE_MODELS.values():
        for i in range(8):
            _insert(state, model_id, alert_id=i, hit=0, abs_err=0.5)
    panel, _ = select_panel(state, cfg, catalog)
    assert len(panel) <= 3


def test_staged_elimination_runs(state, cfg):
    cfg.set("MIN_EVALUATED", "30")
    catalog = ModelCatalog(cfg.catalog_path)
    for name in ("s1", "s2", "s3"):
        catalog.add(name, 1.0)
        for i in range(35):
            _insert(state, name, alert_id=i, hit=1 if name != "s3" else 0, abs_err=0.05)
        _insert_calls(state, name, 35, ok=True)
    survivors = staged_elimination(state, cfg, catalog)
    assert isinstance(survivors, list)
    assert survivors  # it always returns a non-empty candidate pool
