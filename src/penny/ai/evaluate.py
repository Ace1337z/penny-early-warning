"""Forecast evaluation: store predictions, evaluate due horizons, naive baselines (6.11)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from ..util import session_end, to_et
from .panel import HORIZONS, AggregateForecast, ModelForecast

log = logging.getLogger(__name__)

DIRECTION_BAND = 0.01
BASELINE_MODELS = {
    "unchanged": "__baseline_unchanged",
    "momentum": "__baseline_momentum",
    "giveback": "__baseline_giveback",
}
MIN_HORIZON_SECONDS = 300.0  # skip horizons due in under 5 minutes


def horizon_due_ts(ts: float, horizon: str, session: str) -> Optional[float]:
    """Due time for a horizon: +15m, +60m or the end of the current session (6.11)."""
    if horizon == "15m":
        return ts + 15 * 60
    if horizon == "60m":
        return ts + 60 * 60
    if horizon == "session_end":
        end = session_end(datetime.fromtimestamp(ts, tz=timezone.utc))
        return end.timestamp()
    return None


def predicted_direction(predicted: float, price0: float) -> str:
    if not price0:
        return "flat"
    change = predicted / price0 - 1.0
    if change >= DIRECTION_BAND:
        return "up"
    if change <= -DIRECTION_BAND:
        return "down"
    return "flat"


def baseline_prices(price0: float, rise15: float, low4h: Optional[float],
                    horizon_minutes: float) -> dict[str, float]:
    """The three naive predictors scored alongside the models (6.11)."""
    unchanged = price0
    momentum = price0 * (1.0 + rise15 * (horizon_minutes / 15.0))
    if low4h and low4h > 0 and low4h < price0:
        giveback = low4h + 0.5 * (price0 - low4h)
    else:
        giveback = price0
    return {
        "unchanged": max(unchanged, 0.0001),
        "momentum": max(momentum, 0.0001),
        "giveback": max(giveback, 0.0001),
    }


def store_predictions(db, *, alert_id: int, symbol: str, ts: float, session: str,
                      price0: float, forecasts: list[ModelForecast],
                      aggregate: AggregateForecast, active_models: set[str],
                      rise15: float = 0.0, low4h: Optional[float] = None) -> int:
    """Store every model's forecast (active and shadow) plus the three baselines."""
    rows = []
    horizon_minutes = {"15m": 15.0, "60m": 60.0}

    def add(model: str, horizon: str, due: float, price: Optional[float],
            exp_peak: Optional[float], exp_low: Optional[float], active: bool,
            direction: Optional[str] = None) -> None:
        if not price or price <= 0:
            return
        rows.append((
            alert_id, symbol, model, ts, session, price0, horizon, due, price,
            direction or predicted_direction(price, price0), exp_peak, exp_low,
            1 if active else 0,
        ))

    for fc in forecasts:
        if not fc.valid:
            continue
        is_active = fc.model in active_models
        for horizon in HORIZONS:
            due = horizon_due_ts(ts, horizon, session)
            if due is None or due - ts < MIN_HORIZON_SECONDS:
                continue
            add(fc.model, horizon, due, fc.forecast.get(horizon),
                fc.expected_peak, fc.expected_low, is_active)

    # Baselines use the same horizons and the same evaluation machinery.
    for name, model_id in BASELINE_MODELS.items():
        for horizon in HORIZONS:
            due = horizon_due_ts(ts, horizon, session)
            if due is None or due - ts < MIN_HORIZON_SECONDS:
                continue
            minutes = 15.0 if horizon == "15m" else (60.0 if horizon == "60m" else
                                                     max(15.0, (due - ts) / 60.0))
            prices = baseline_prices(price0, rise15, low4h, minutes)
            add(model_id, horizon, due, prices[name], None, None, False)

    if rows:
        db.executemany(
            "INSERT INTO predictions(alert_id, symbol, model, ts, session, price0, horizon, "
            "due_ts, pred_price, pred_dir, exp_peak, exp_low, active) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def evaluate_due(db, price_lookup: Callable[[str, float], Optional[float]],
                 now: Optional[float] = None, max_poll_gap: float = 120.0) -> int:
    """Evaluate pending predictions whose horizon has passed. Returns rows evaluated."""
    now = now or time.time()
    pending = db.query(
        "SELECT * FROM predictions WHERE status='pending' AND due_ts IS NOT NULL AND due_ts<=?",
        (now,))
    evaluated = 0
    for row in pending:
        actual = price_lookup(row["symbol"], row["due_ts"])
        if actual is None or actual <= 0:
            db.execute("UPDATE predictions SET status='unavailable' WHERE id=?", (row["id"],))
            continue
        price0 = row["price0"] or 0
        actual_dir = predicted_direction(actual, price0) if price0 else "flat"
        hit = 1 if (row["pred_dir"] == actual_dir) else 0
        abs_err = abs(row["pred_price"] - actual) / actual if actual else None
        peak_err = None
        low_err = None
        actual_high = None
        actual_low = None
        if row["horizon"] == "session_end":
            high, low = _actual_extremes(db, row["symbol"], row["ts"], row["due_ts"], price_lookup)
            actual_high, actual_low = high, low
            if high and row["exp_peak"]:
                peak_err = abs(row["exp_peak"] - high) / high
            if low and row["exp_low"]:
                low_err = abs(row["exp_low"] - low) / low
        db.execute(
            "UPDATE predictions SET actual_price=?, actual_dir=?, actual_high=?, actual_low=?, "
            "hit=?, abs_err=?, peak_err=?, low_err=?, status='evaluated' WHERE id=?",
            (actual, actual_dir, actual_high, actual_low, hit, abs_err, peak_err, low_err,
             row["id"]))
        evaluated += 1
    return evaluated


def _actual_extremes(db, symbol: str, start_ts: float, due_ts: float,
                     price_lookup: Callable[[str, float], Optional[float]]):
    """High/low since the alert, from stored evaluated points when candles are unavailable."""
    rows = db.query(
        "SELECT actual_price FROM predictions WHERE symbol=? AND ts>=? AND ts<=? "
        "AND actual_price IS NOT NULL", (symbol, start_ts, due_ts))
    prices = [r["actual_price"] for r in rows if r["actual_price"]]
    if not prices:
        return None, None
    return max(prices), min(prices)


def mark_pending_unavailable(db, reason: str = "restore") -> int:
    """After a restore, forecasts that fell due while down are never guessed (6.18)."""
    cur = db.execute("UPDATE predictions SET status='unavailable' WHERE status='pending'")
    return cur.rowcount if cur else 0


def pending_count(db) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM predictions WHERE status='pending'", default=0))
