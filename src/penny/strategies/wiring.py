"""Turn engine state into the Bundle the strategy detectors read.

Isolated here so the engine's wiring stays readable and so the mapping from
`Metrics` to a detector's input can be tested without a running engine.

Two things matter here and both are easy to get wrong:

  - A detector must never receive a stale or invented value. Where the engine
    does not have the input, the field stays None and the detector returns
    INSUFFICIENT_DATA. A quiet guess would invent a setup that did not happen.
  - The thresholds are copied from config once per call, not read inside the
    detectors, so a detector is a pure function of its Bundle.
"""

from __future__ import annotations

from typing import Any, Optional

from .base import Bundle, StrategyGate
from .gate import evaluate_gate
from ..util import now_et


def metrics_to_bundle(m, *, enr=None, cfg=None, now=None, history=None,
                      daily_close=None) -> Bundle:
    """Assemble the Bundle for one symbol.

    `enr` is the Enrichment (or None if this symbol has not been enriched).
    `history` is the prior completed sessions for this symbol, oldest first.
    """
    frow = getattr(enr, "finviz_row", None)
    frow = frow if isinstance(frow, dict) else {}

    b = Bundle(
        symbol=m.symbol,
        price=m.price,
        prev_close=m.prev_close,
        open_price=_num(frow.get("Open")),
        day_high=m.day_high,
        day_low=getattr(m, "day_low", None),
        cum_volume=m.cum_volume,
        pct_vs_close=m.pct_vs_close,
        bid=getattr(m, "bid", None),
        ask=getattr(m, "ask", None),
        spread_pct=getattr(m, "spread_pct", None),
        # None on a Finviz-only feed, which publishes no print count. The reality
        # gate treats that as INSUFFICIENT_DATA rather than guessing: an invented
        # trade count would clear or block the gate on a made-up number.
        trades_15m=getattr(m, "trades_15m", None),
        rise=dict(m.rise or {}),
        volx=dict(m.volx or {}),
        dollar_vol=dict(m.dollar_vol or {}),
        vwap=m.vwap,
        from_high=m.from_high,
        minutes_since_low=m.minutes_since_low,
        accel=float(getattr(m, "accel", 1.0) or 1.0),
        minutes_tracked=_minutes_tracked(m),
        session=getattr(m, "session", "regular"),
        now_et=now or now_et(),
        float_shares=m.float_shares,
        market_cap=m.market_cap,
        # Thresholds copied from config once per call (same pattern as
        # dollar_level): the mechanism detectors stay pure Bundle functions.
        dollar_level=cfg.float("DOLLAR_LEVEL", 1.00) if cfg else 1.00,
        short_float_min=cfg.float("SHORT_FLOAT_MIN", 8.0) if cfg else 8.0,
        cause_max_age_h=cfg.float("CAUSE_MAX_AGE_H", 24.0) if cfg else 24.0,
    )

    if enr is not None:
        b.bars_1m = enr.bars_1m
        b.news = enr.news
        b.filings = enr.filings
        b.float_shares = enr.float_shares or b.float_shares
        b.market_cap = enr.market_cap or b.market_cap
        b.sector = enr.sector or ""
        b.plan = enr.plan
        b.short_pct = _short_pct(enr)
        b.short_ratio = _short_ratio(enr)
        b.sector_peers = getattr(enr, "sector_peers", None)

    if history:
        b.daily_history = history
    elif daily_close is not None:
        b.daily_history = daily_close

    # Supernova phase (Spec 2.3). Computed here, once, from the numbers the
    # Bundle already carries, so every consumer reads the same label rather than
    # re-deriving the shape. It is pure and cannot fail the gate.
    try:
        from .supernova import classify_bundle
        b.supernova_phase = classify_bundle(b).phase
    except Exception as exc:  # noqa: BLE001
        # A phase that cannot be computed must never take the gate down. The
        # field stays None and the confirmation machine simply does not arm.
        import logging
        logging.getLogger(__name__).debug("supernova phase failed: %s", exc)

    # Catalyst tier + sizing (Spec 3.3). Derived from the same ranked news the
    # cause line uses, so the tier can never name a driver the alert does not.
    try:
        from .news_cause import rank_catalysts
        from .routing import catalyst_tier, size_multiplier
        _, ranked, _ = rank_catalysts(b)
        b.catalyst_tier = catalyst_tier(b, ranked)
        b.size_multiplier = size_multiplier(b.catalyst_tier)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).debug("catalyst tier failed: %s", exc)

    # Apply the sizing multiplier to the trade plan's share count (Spec 3.3).
    # The plan is scaled on a copy so the enrichment's own plan is untouched and
    # a Tier 3 setup cannot be sized at full risk.
    if b.plan is not None and b.size_multiplier < 1.0:
        try:
            b.plan = _scaled_plan(b.plan, b.size_multiplier)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).debug("plan scaling failed: %s", exc)

    return b


def _scaled_plan(plan, multiplier: float):
    """A copy of the plan with its share count scaled by ``multiplier`` (Spec 3.3)."""
    import copy
    scaled = copy.copy(plan)
    try:
        scaled.shares = max(0, int((getattr(plan, "shares", 0) or 0) * multiplier))
    except Exception:  # noqa: BLE001
        return plan
    return scaled


def _num(value) -> Optional[float]:
    """Finviz columns arrive as strings with '%' and units attached."""
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _minutes_tracked(m) -> float:
    """How long the rolling state has held this symbol, in minutes.

    `Metrics` carries `age_seconds` (seconds since the symbol was first seen
    this session); `Bundle.minutes_tracked` is that expressed in minutes.

    Read with `getattr` on purpose: this wiring is handed lightweight stand-ins
    in tests and any future Metrics variant, and an AttributeError here takes
    the whole gate down with it. That is exactly how the bug this replaces went
    unnoticed -- `_run_gate` catches the exception, logs a warning and returns
    no gate at all, so with the gate mandatory the failure looked like a quiet
    day, and with it advisory it simply vanished.
    """
    age = _num(getattr(m, "age_seconds", None))
    return (age / 60.0) if age else 0.0


def _short_pct(enr) -> Optional[float]:
    """Finviz spells this Short Float as a percent string."""
    row = getattr(enr, "finviz_row", None)
    if not isinstance(row, dict):
        return None
    for key in ("Short Float", "ShortFloat", "short_float"):
        v = row.get(key)
        if v in (None, "", "-"):
            continue
        try:
            return float(str(v).replace("%", "").strip())
        except (TypeError, ValueError):
            continue
    return None


def _short_ratio(enr) -> Optional[float]:
    row = getattr(enr, "finviz_row", None)
    if not isinstance(row, dict):
        return None
    for key in ("Short Ratio", "ShortRatio", "short_ratio"):
        v = row.get(key)
        if v in (None, "", "-"):
            continue
        try:
            return float(str(v).strip())
        except (TypeError, ValueError):
            continue
    return None


def run_gate(m, *, enr=None, cfg=None, now=None, history=None) -> tuple[StrategyGate, Bundle]:
    """Evaluate the gate for one symbol. Returns the gate and the Bundle it read."""
    b = metrics_to_bundle(m, enr=enr, cfg=cfg, now=now, history=history)
    gate = evaluate_gate(
        b,
        min_score=cfg.float("GATE_MIN_SCORE", 55.0) if cfg else 55.0,
        require_catalyst=cfg.bool("CAUSE_REQUIRED", False) if cfg else False,
        time_filter=cfg.bool("TIME_FILTER_ENABLED", True) if cfg else True,
        **gate_limits(cfg),
    )
    return gate, b


def gate_limits(cfg) -> dict:
    """The gate thresholds that come from config, read once per evaluation."""
    if cfg is None:
        return {}
    return {
        "spread_max_pct": cfg.float("SPREAD_MAX_PCT", 1.5),
        "min_dollar_vol_15m": cfg.float("MIN_DOLLAR_VOL_15M", 300000.0),
        "min_avg_trade_size": cfg.float("MIN_AVG_TRADE_SIZE", 200.0),
        "min_trades_15m": cfg.int("MIN_TRADES_15M", 30),
        "exit_liq_max_sec": cfg.int("EXIT_LIQ_500_MAX_SEC", 300),
        "exit_liq_max_slip_pct": cfg.float("EXIT_LIQ_MAX_SLIP_PCT", 1.0),
    }
