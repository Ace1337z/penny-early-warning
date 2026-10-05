"""Model leaderboard, eligibility rules and automatic panel selection (6.11, 6.19)."""

from __future__ import annotations

import logging
import math
import statistics
import time
from typing import Iterable, Optional

from .evaluate import BASELINE_MODELS
from .panel import HORIZONS

log = logging.getLogger(__name__)

BASELINE_IDS = set(BASELINE_MODELS.values())
BASELINE_LABELS = {v: k for k, v in BASELINE_MODELS.items()}

MAPE_TARGET = 0.25
BEAT_MARGIN = 0.05
MAX_LATENCY_MS = 60_000.0
MIN_VALID_JSON = 0.90
ELIMINATION_MIN_VALID_JSON = 0.80
TIE_MARGIN = 0.02


def wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def composite_from(hit_rate: float, mape: Optional[float]) -> float:
    m = mape if mape is not None else 1.0
    return 0.6 * hit_rate + 0.4 * (1.0 - min(m / MAPE_TARGET, 1.0))


def _window_clause(window_days: int, params: list) -> str:
    if window_days and window_days > 0:
        params.append(time.time() - window_days * 86400)
        return " AND ts>=?"
    return ""


def eval_rows(db, model: str, alert_ids: Optional[Iterable[int]] = None,
              window_days: int = 0) -> list:
    params: list = [model]
    sql = "SELECT * FROM predictions WHERE model=? AND status='evaluated'"
    sql += _window_clause(window_days, params)
    ids = list(alert_ids) if alert_ids is not None else None
    if ids is not None:
        if not ids:
            return []
        sql += " AND alert_id IN (%s)" % ",".join("?" * len(ids))
        params += ids
    return db.query(sql, params)


def composite_of_rows(rows: list) -> Optional[float]:
    if not rows:
        return None
    hits = sum(1 for r in rows if r["hit"])
    errs = [r["abs_err"] for r in rows if r["abs_err"] is not None]
    mape = sum(errs) / len(errs) if errs else None
    return composite_from(hits / len(rows), mape)


def model_stats(db, model: str, window_days: int = 0,
                alert_ids: Optional[Iterable[int]] = None) -> dict:
    rows = eval_rows(db, model, alert_ids=alert_ids, window_days=window_days)
    n = len(rows)
    alerts = {r["alert_id"] for r in rows}
    hits = sum(1 for r in rows if r["hit"])
    errs = [r["abs_err"] for r in rows if r["abs_err"] is not None]
    mape = sum(errs) / len(errs) if errs else None
    hit_rate = hits / n if n else 0.0
    lo, hi = wilson_interval(hits, n)

    call_params: list = [model]
    calls = db.query(
        "SELECT ok, latency_ms, tokens FROM ai_calls WHERE model=?" + _window_clause(window_days, call_params),
        call_params)
    total_calls = len(calls)
    ok_calls = sum(1 for c in calls if c["ok"])
    latencies = [c["latency_ms"] for c in calls if c["latency_ms"] is not None and c["ok"]]
    tokens = [c["tokens"] for c in calls if c["tokens"]]
    valid_json_rate = ok_calls / total_calls if total_calls else 0.0
    median_latency = statistics.median(latencies) if latencies else None
    mean_tokens = sum(tokens) / len(tokens) if tokens else 0.0

    return {
        "model": model,
        "evaluated": n,
        "alerts": len(alerts),
        "hit_rate": hit_rate,
        "hit_ci": (lo, hi),
        "mape": mape,
        "composite": composite_from(hit_rate, mape) if n else None,
        "valid_json_rate": valid_json_rate,
        "calls": total_calls,
        "median_latency_ms": median_latency,
        "mean_tokens": mean_tokens,
    }


def baseline_stats(db, window_days: int = 0, alert_ids: Optional[Iterable[int]] = None) -> dict:
    out = {}
    for name, model_id in BASELINE_MODELS.items():
        rows = eval_rows(db, model_id, alert_ids=alert_ids, window_days=window_days)
        comp = composite_of_rows(rows)
        out[name] = {"composite": comp, "rows": len(rows)}
    return out


def best_baseline_composite(db, window_days: int = 0,
                            alert_ids: Optional[Iterable[int]] = None) -> Optional[float]:
    stats = baseline_stats(db, window_days=window_days, alert_ids=alert_ids)
    values = [s["composite"] for s in stats.values() if s["composite"] is not None]
    return max(values) if values else None


def split_alerts(db, model: str, window_days: int = 0) -> tuple[list[int], list[int]]:
    """Older 70% / newest 30% of the alerts this model evaluated (overfitting guard)."""
    rows = db.query(
        "SELECT DISTINCT alert_id, ts FROM predictions WHERE model=? AND status='evaluated'"
        + _window_clause(window_days, []),
        ([model] + ([time.time() - window_days * 86400] if window_days else [])))
    pairs = sorted({(r["alert_id"], r["ts"]) for r in rows}, key=lambda x: x[1])
    if not pairs:
        return [], []
    cut = int(len(pairs) * 0.7)
    cut = max(1, min(cut, len(pairs) - 1)) if len(pairs) > 1 else len(pairs)
    old = [a for a, _ in pairs[:cut]]
    new = [a for a, _ in pairs[cut:]]
    return old, new


def eligibility(db, model: str, cfg, window_days: int = 0) -> tuple[bool, str]:
    """Eligibility rules in 6.11, including the 70/30 overfitting guard."""
    min_evaluated = cfg.int("MIN_EVALUATED", 100)
    stats = model_stats(db, model, window_days=window_days)
    if stats["alerts"] < min_evaluated:
        return False, f"only {stats['alerts']}/{min_evaluated} evaluated alerts"
    if stats["valid_json_rate"] < MIN_VALID_JSON:
        return False, f"valid-JSON rate {stats['valid_json_rate']:.0%} < 90%"
    if stats["median_latency_ms"] is not None and stats["median_latency_ms"] > MAX_LATENCY_MS:
        return False, f"median latency {stats['median_latency_ms'] / 1000:.0f}s > 60s"
    if stats["composite"] is None:
        return False, "no evaluated forecasts"

    old, new = split_alerts(db, model, window_days=window_days)
    overall_base = best_baseline_composite(db, window_days=window_days)
    if overall_base is None:
        return False, "no baseline scores yet"
    if stats["composite"] < overall_base + BEAT_MARGIN:
        return False, (f"composite {stats['composite']:.3f} does not beat best baseline "
                       f"{overall_base:.3f} by 0.05")
    if new:
        model_new = composite_of_rows(eval_rows(db, model, alert_ids=new, window_days=window_days))
        base_new = best_baseline_composite(db, window_days=window_days, alert_ids=new)
        if model_new is None or base_new is None or model_new < base_new + BEAT_MARGIN:
            return False, "does not beat the best baseline on the newest 30%"
    return True, "eligible"


def leaderboard(db, cfg, catalog=None, window_days: Optional[int] = None) -> list[dict]:
    window_days = cfg.int("SELECTION_WINDOW_DAYS", 0) if window_days is None else window_days
    models = [m for m in _candidate_universe(db, cfg, catalog)]
    rows = []
    for model in models:
        stats = model_stats(db, model, window_days=window_days)
        ok, reason = eligibility(db, model, cfg, window_days=window_days)
        stats["eligible"] = ok
        stats["reason"] = reason
        stats["multiplier"] = catalog.multiplier_of(model) if catalog else 1.0
        rows.append(stats)

    # Baselines shown for reference.
    for name, model_id in BASELINE_MODELS.items():
        stats = model_stats(db, model_id, window_days=window_days)
        stats["model"] = model_id
        stats["eligible"] = False
        stats["reason"] = "baseline"
        stats["multiplier"] = 0.0
        stats["is_baseline"] = True
        stats["label"] = f"baseline:{name}"
        rows.append(stats)

    rows.sort(key=lambda r: (r["composite"] is None, -(r["composite"] or 0)))
    return rows


def _candidate_universe(db, cfg, catalog) -> list[str]:
    panel = db.kv_get("active_panel") or cfg.list("AI_MODELS")
    candidates = db.kv_get("candidates") or []
    out: list[str] = []
    for model in list(panel) + list(candidates):
        if model and model not in out and model not in BASELINE_IDS:
            out.append(model)
    if catalog:
        for model in catalog.all_ids():
            if model not in out and model not in BASELINE_IDS:
                out.append(model)
    return out


def staged_elimination(db, cfg, catalog) -> list[str]:
    """Drop the weaker half and weak valid-JSON models at each stage (6.19)."""
    stages = cfg.int_list("MODEL_HALVING_STAGES") or [30, 60, 100]
    stage = int(db.kv_get("elimination_stage") or 0)
    candidates = db.kv_get("candidates")
    if not candidates:
        candidates = catalog.pool(cfg.float("AI_MAX_MULTIPLIER", 4),
                                  cfg.list("AI_PREMIUM_MODELS"))
        db.kv_set("candidates", candidates)
        db.kv_set("elimination_stage", 0)
        return candidates
    if stage >= len(stages):
        return candidates

    threshold = stages[stage]
    stats = {m: model_stats(db, m) for m in candidates}
    if any(s["alerts"] < threshold for s in stats.values()):
        return candidates

    scored = [(m, s) for m, s in stats.items() if s["composite"] is not None]
    scored.sort(key=lambda kv: kv[1]["composite"], reverse=True)
    keep = [m for m, s in scored if s["valid_json_rate"] >= ELIMINATION_MIN_VALID_JSON]
    half = max(3, len(keep) // 2)
    survivors = keep[:half] if len(keep) > 3 else keep
    dropped = [m for m in candidates if m not in survivors]
    db.kv_set("candidates", survivors)
    db.kv_set("eliminated", sorted(set((db.kv_get("eliminated") or []) + dropped)))
    db.kv_set("elimination_stage", stage + 1)
    log.info("staged elimination at %d alerts: dropped %s", threshold, dropped)
    return survivors


def select_panel(db, cfg, catalog, window_days: Optional[int] = None,
                 dry_run: bool = False) -> tuple[list[str], list[dict]]:
    """The three eligible models with the highest composite score (ties: lower MAPE)."""
    window_days = cfg.int("SELECTION_WINDOW_DAYS", 0) if window_days is None else window_days
    rows = [r for r in leaderboard(db, cfg, catalog, window_days=window_days)
            if not r.get("is_baseline")]

    def sort_key(r):
        return (-(r["composite"] or 0.0), r["mape"] if r["mape"] is not None else 9.9,
                r.get("multiplier", 1.0))

    eligible = sorted([r for r in rows if r.get("eligible")], key=sort_key)
    if len(eligible) < 3:
        return list(db.kv_get("active_panel") or cfg.list("AI_MODELS")), rows

    chosen: list[dict] = []
    for r in eligible:
        if len(chosen) >= 3:
            break
        # When composite scores are within 0.02, the lower multiplier wins (6.19).
        if chosen and abs((r["composite"] or 0) - (chosen[-1]["composite"] or 0)) <= TIE_MARGIN:
            if r.get("multiplier", 1.0) < chosen[-1].get("multiplier", 1.0):
                chosen[-1] = r
                continue
        chosen.append(r)
    panel = [r["model"] for r in chosen]
    return panel, rows


def apply_selection(db, cfg, catalog, telegram=None) -> tuple[list[str], bool]:
    """Apply automatic or manual selection. Returns (panel, changed)."""
    if cfg.raw("MODEL_SELECTION").strip().lower() == "manual":
        return list(db.kv_get("active_panel") or cfg.list("AI_MODELS")), False

    current = list(db.kv_get("active_panel") or cfg.list("AI_MODELS"))
    panel, rows = select_panel(db, cfg, catalog)
    if not panel or panel == current:
        return current, False

    db.kv_set("active_panel", panel)
    db.kv_set("last_selection_time", time.time())
    now = time.time()
    for r in rows:
        if r.get("is_baseline"):
            continue
        db.execute(
            "INSERT INTO leaderboard_history(ts, model, stage, evaluated, hit_rate, mape, "
            "composite, valid_json_rate, latency_ms, multiplier, baseline) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now, r["model"], int(db.kv_get("elimination_stage") or 0), r["alerts"],
             r["hit_rate"], r["mape"], r["composite"], r["valid_json_rate"],
             r["median_latency_ms"], r.get("multiplier", 1.0),
             best_baseline_composite(db)))
    if telegram:
        telegram.send("MODEL PANEL CHANGED\n" + leaderboard_text(db, cfg, catalog))
    return panel, True


def leaderboard_text(db, cfg, catalog=None, limit: int = 15) -> str:
    rows = leaderboard(db, cfg, catalog)
    active = set(db.kv_get("active_panel") or cfg.list("AI_MODELS"))
    lines = ["MODEL LEADERBOARD (composite = 0.6 x direction hit + 0.4 x (1 - MAPE/25%))"]
    shown = 0
    for r in rows:
        if r.get("is_baseline"):
            continue
        if shown >= limit:
            break
        shown += 1
        comp = f"{r['composite']:.3f}" if r["composite"] is not None else "n/a"
        mape = f"{r['mape'] * 100:.1f}%" if r["mape"] is not None else "n/a"
        lat = f"{r['median_latency_ms'] / 1000:.1f}s" if r["median_latency_ms"] else "n/a"
        mark = "*" if r["model"] in active else " "
        lines.append(
            f"{mark} {r['model']:<28} {r.get('multiplier', 1.0):>4.2f}x  n={r['alerts']:<4} "
            f"hit {r['hit_rate'] * 100:>5.1f}% [{r['hit_ci'][0] * 100:.0f}-{r['hit_ci'][1] * 100:.0f}]  "
            f"MAPE {mape:>6}  comp {comp:>5}  json {r['valid_json_rate'] * 100:>3.0f}%  lat {lat}")
    lines.append("")
    lines.append("baselines: " + ", ".join(
        f"{name} {s['composite']:.3f}" if s["composite"] is not None else f"{name} n/a"
        for name, s in baseline_stats(db).items()))
    lines.append("active panel: " + ", ".join(sorted(active)))
    lines.append("panel multiplier: "
                 f"{sum(catalog.multiplier_of(m) if catalog else 1.0 for m in active):.2f}x")
    if not any(r.get("eligible") for r in rows):
        lines.append("No model qualifies yet; forecasts are labelled unvalidated.")
    return "\n".join(lines)
