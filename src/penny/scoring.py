"""Rolling-state scoring engine: windows, tiers, phase and score (documentation 6.5)."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

from .util import ET, clamp, compact, now_et, safe_div

log = logging.getLogger(__name__)

TIER_NONE = 0
TIER_WATCH = 1
TIER_EARLY = 2
TIER_CONFIRMED = 3

PHASE_BUILDING = "BUILDING"
PHASE_EXTENDED = "EXTENDED"
PHASE_FADING = "FADING"

WINDOWS = (5, 15, 60, 240)


@dataclass
class Sample:
    ts: float          # epoch seconds
    price: float
    cum_volume: float


@dataclass
class Metrics:
    symbol: str
    ts: float
    price: float
    prev_close: Optional[float] = None
    cum_volume: float = 0.0
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread_pct: Optional[float] = None
    market_cap: Optional[float] = None
    float_shares: Optional[float] = None
    halted: bool = False
    session: str = "regular"

    rise: dict[int, float] = field(default_factory=dict)
    vol_added: dict[int, float] = field(default_factory=dict)
    volx: dict[int, float] = field(default_factory=dict)
    dollar_vol: dict[int, float] = field(default_factory=dict)

    accel: float = 1.0
    vwap: Optional[float] = None
    from_high: Optional[float] = None
    day_high: Optional[float] = None
    minutes_since_low: Optional[float] = None
    pct_vs_close: Optional[float] = None

    tier: int = TIER_NONE
    score: float = 0.0
    phase: str = PHASE_BUILDING
    baseline: float = 300.0
    warm: bool = False
    age_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "price": round(self.price, 6),
            "prev_close": self.prev_close,
            "cum_volume": self.cum_volume,
            "rise": {k: round(v, 4) for k, v in self.rise.items()},
            "volx": {k: round(v, 3) for k, v in self.volx.items()},
            "dollar_vol": {k: round(v, 2) for k, v in self.dollar_vol.items()},
            "accel": round(self.accel, 3),
            "vwap": round(self.vwap, 6) if self.vwap else None,
            "from_high": round(self.from_high, 4) if self.from_high is not None else None,
            "day_high": self.day_high,
            "pct_vs_close": round(self.pct_vs_close, 4) if self.pct_vs_close is not None else None,
            "tier": self.tier,
            "score": round(self.score, 1),
            "phase": self.phase,
            "baseline": round(self.baseline, 2),
            "halted": self.halted,
        }


@dataclass
class BuildSignal:
    """A stock that is accumulating before it reaches the alert bar."""

    metrics: "Metrics"
    score: float = 0.0            # 0-100 progress toward the tier-2 gates
    stage: str = "WARMING"        # WARMING / BUILDING / ACCELERATING / COILED
    missing: list[str] = field(default_factory=list)
    above_vwap: bool = False


class SymbolState:
    """Time-ordered samples and derived accumulators for one symbol."""
    def __init__(self, symbol: str, sample_seconds: float = 55.0, retain_hours: float = 6.0):
        self.symbol = symbol
        self.sample_seconds = sample_seconds
        self.retain_seconds = retain_hours * 3600.0
        self.samples: list[Sample] = []
        self.day_high: float = 0.0
        self.day_low: float = 0.0
        self.last_seen: float = 0.0
        self.avg_volume: Optional[float] = None      # average daily volume
        self._vwap_pv: float = 0.0
        self._vwap_vol: float = 0.0
        self._prev_cum: Optional[float] = None
        self._day: Optional[str] = None
        self.halted: bool = False
        self.halt_resume_pending: bool = False
        self.last_tier: int = TIER_NONE
        self.last_alert_ts: float = 0.0
        self.last_alert_tier: int = 0
        self.first_seen: float = 0.0
        self.last_price: Optional[float] = None

    # -- lifecycle ----------------------------------------------------------
    def reset(self, day: Optional[str] = None) -> None:
        self.samples.clear()
        self.day_high = 0.0
        self.day_low = 0.0
        self._vwap_pv = 0.0
        self._vwap_vol = 0.0
        self._prev_cum = None
        self._day = day
        self.last_tier = TIER_NONE
        self.last_alert_tier = 0
        self.last_alert_ts = 0.0
        self.halt_resume_pending = False

    def add_sample(self, ts: float, price: float, cum_volume: float) -> None:
        """Add or replace a sample. A faster update replaces the last one."""
        if price is None or price <= 0:
            return
        cum_volume = cum_volume or 0.0

        day = now_et().strftime("%Y-%m-%d")
        if self._day is None:
            self._day = day
        elif day != self._day:
            log.info("%s: new day detected, resetting state", self.symbol)
            self.reset(day)

        if self._prev_cum is not None and cum_volume < self._prev_cum * 0.5:
            log.info("%s: cumulative volume dropped (%s -> %s); resetting state",
                     self.symbol, self._prev_cum, cum_volume)
            self.reset(day)

        # VWAP estimate: price x volume added between polls.
        if self._prev_cum is not None:
            delta = cum_volume - self._prev_cum
            if delta > 0:
                self._vwap_pv += price * delta
                self._vwap_vol += delta

        if self.samples and (ts - self.samples[-1].ts) < self.sample_seconds:
            self.samples[-1] = Sample(ts, price, cum_volume)
        else:
            self.samples.append(Sample(ts, price, cum_volume))

        self._prev_cum = cum_volume
        self.last_seen = ts
        self.last_price = price
        if self.first_seen == 0.0:
            self.first_seen = ts
        self.day_high = max(self.day_high, price)
        self.day_low = price if self.day_low == 0 else min(self.day_low, price)

        cutoff = ts - self.retain_seconds
        if self.samples and self.samples[0].ts < cutoff:
            i = 0
            while i < len(self.samples) and self.samples[i].ts < cutoff:
                i += 1
            del self.samples[:max(0, i - 1)]

    # -- window helpers -----------------------------------------------------
    def _window_start_index(self, ts: float, minutes: int) -> int:
        target = ts - minutes * 60.0
        idx = 0
        for i, s in enumerate(self.samples):
            if s.ts <= target:
                idx = i
            else:
                break
        return idx

    def rise(self, minutes: int) -> Optional[float]:
        if not self.samples:
            return None
        idx = self._window_start_index(self.samples[-1].ts, minutes)
        low = min(s.price for s in self.samples[idx:])
        cur = self.samples[-1].price
        if low <= 0:
            return None
        return cur / low - 1.0

    def volume_added(self, minutes: int) -> float:
        if not self.samples:
            return 0.0
        idx = self._window_start_index(self.samples[-1].ts, minutes)
        return max(0.0, self.samples[-1].cum_volume - self.samples[idx].cum_volume)

    def minutes_covered(self, minutes: int) -> float:
        if len(self.samples) < 2:
            return 0.0
        idx = self._window_start_index(self.samples[-1].ts, minutes)
        span = (self.samples[-1].ts - self.samples[idx].ts) / 60.0
        return max(0.0, min(span, float(minutes)))

    def history_minutes(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return (self.samples[-1].ts - self.samples[0].ts) / 60.0

    def baseline(self, fallback: float = 300.0, min_per_min: float = 20.0) -> float:
        if self.avg_volume and self.avg_volume > 0:
            return max(self.avg_volume / 960.0, min_per_min)
        return float(fallback)

    def accel(self, base: float) -> float:
        if self.history_minutes() < 10:
            return 1.0
        last5 = self.volume_added(5)
        prior5 = self.volume_added(10) - last5
        denom = max(prior5, base * 5.0)
        if denom <= 0:
            return 1.0
        return last5 / denom

    def vwap_estimate(self) -> Optional[float]:
        if self._vwap_vol <= 0:
            return None
        return self._vwap_pv / self._vwap_vol

    def minutes_since_low(self, hours: float = 4.0) -> Optional[float]:
        if not self.samples:
            return None
        cutoff = self.samples[-1].ts - hours * 3600.0
        window = [s for s in self.samples if s.ts >= cutoff] or self.samples
        low = min(window, key=lambda s: s.price)
        return (self.samples[-1].ts - low.ts) / 60.0


class Scorer:
    """Computes metrics, tiers, phase and score for symbols from configuration."""

    def __init__(self, cfg, clock=None):
        self.cfg = cfg
        self.clock = clock or time.time
        self.sample_seconds = cfg.float("SAMPLE_SECONDS", 55.0)
        self.state_hours = cfg.float("STATE_HOURS", 6.0)
        self.states: dict[str, SymbolState] = {}
        self.warmup_cycles = cfg.int("WARMUP_CYCLES", 2)
        self.cycles = 0
        self.reset_at = self.clock()

    # -- state --------------------------------------------------------------
    def state(self, symbol: str) -> SymbolState:
        st = self.states.get(symbol)
        if st is None:
            st = SymbolState(symbol, self.sample_seconds, self.state_hours)
            self.states[symbol] = st
        return st

    def set_avg_volume(self, symbol: str, avg_volume: Optional[float]) -> None:
        if avg_volume and avg_volume > 0:
            self.state(symbol).avg_volume = float(avg_volume)

    def reset_all(self) -> None:
        for st in self.states.values():
            st.reset()
        self.cycles = 0
        self.reset_at = self.clock()

    def prune(self, max_age_seconds: float = 2 * 3600.0) -> int:
        now = self.clock()
        dead = [s for s, st in self.states.items() if st.last_seen and now - st.last_seen > max_age_seconds]
        for s in dead:
            del self.states[s]
        return len(dead)

    def mark_halted(self, symbol: str, halted: bool) -> None:
        st = self.state(symbol)
        if st.halted and not halted:
            st.halt_resume_pending = True
            st.reset()
            st.last_tier = TIER_NONE
        st.halted = halted

    @property
    def warm(self) -> bool:
        return self.cycles > self.warmup_cycles

    # -- per-symbol metrics -------------------------------------------------
    def compute(self, quote: dict, session: str) -> Optional[Metrics]:
        symbol = quote["symbol"]
        st = self.state(symbol)
        price = quote.get("price")
        if not price or price <= 0:
            return None

        st.add_sample(quote["ts"], price, quote.get("cum_volume") or 0.0)
        if quote.get("halted"):
            st.halted = True
        elif st.halted:
            st.halt_resume_pending = True
            st.halted = False

        m = Metrics(symbol=symbol, ts=quote["ts"], price=price, session=session)
        m.prev_close = quote.get("prev_close")
        m.cum_volume = quote.get("cum_volume") or 0.0
        m.bid = quote.get("bid")
        m.ask = quote.get("ask")
        m.market_cap = quote.get("market_cap")
        m.float_shares = quote.get("float_shares")
        m.halted = bool(quote.get("halted"))
        if m.bid and m.ask and m.ask > 0:
            m.spread_pct = safe_div(m.ask - m.bid, m.ask) * 100.0

        m.baseline = st.baseline(self.cfg.float("BASELINE_FALLBACK", 300.0),
                                 self.cfg.float("BASELINE_MIN_PER_MIN", 20.0))

        for w in WINDOWS:
            r = st.rise(w)
            m.rise[w] = r if r is not None else 0.0
            added = st.volume_added(w)
            m.vol_added[w] = added
            covered = max(1.0, st.minutes_covered(w)) if added > 0 else 1.0
            m.volx[w] = safe_div(added, m.baseline * covered, 0.0)
            m.dollar_vol[w] = added * price

        m.accel = st.accel(m.baseline)
        m.vwap = st.vwap_estimate()
        high = max(st.day_high, quote.get("high_price") or 0.0)
        m.day_high = high if high > 0 else None
        if m.day_high and m.day_high > 0:
            m.from_high = price / m.day_high - 1.0
        m.minutes_since_low = st.minutes_since_low(4.0)
        if m.prev_close and m.prev_close > 0:
            m.pct_vs_close = price / m.prev_close - 1.0

        m.tier = self.tier_for(m)
        m.score = self.score_for(m)
        m.phase = self.phase_for(m)
        m.age_seconds = quote["ts"] - st.first_seen if st.first_seen else 0.0
        m.warm = self.warm
        return m

    # -- tiers --------------------------------------------------------------
    def tier_for(self, m: Metrics) -> int:
        cfg = self.cfg
        above_vwap = m.vwap is not None and m.price > m.vwap
        dv15 = m.dollar_vol.get(15, 0.0)
        rise15 = m.rise.get(15, 0.0)
        rise60 = m.rise.get(60, 0.0)
        volx15 = m.volx.get(15, 0.0)
        from_high = m.from_high if m.from_high is not None else 0.0

        if (rise60 >= cfg.float("TIER3_RISE60", 15) / 100.0
                and volx15 >= cfg.float("TIER3_VOLX15", 20)
                and m.accel >= cfg.float("TIER3_ACCEL", 1.0)
                and above_vwap
                and from_high >= cfg.float("TIER3_FROM_HIGH", -8) / 100.0
                and dv15 >= cfg.float("TIER3_DOLLAR15", 25000)):
            return TIER_CONFIRMED

        if ((rise60 >= cfg.float("TIER2_RISE60", 8) / 100.0
             or rise15 >= cfg.float("TIER2_RISE15", 5) / 100.0)
                and volx15 >= cfg.float("TIER2_VOLX15", 10)
                and dv15 >= cfg.float("TIER2_DOLLAR15", 10000)
                and above_vwap):
            return TIER_EARLY

        if (rise15 >= cfg.float("TIER1_RISE15", 3) / 100.0
                and volx15 >= cfg.float("TIER1_VOLX15", 3)
                and dv15 >= cfg.float("TIER1_DOLLAR15", 3000)):
            return TIER_WATCH

        return TIER_NONE

    def score_for(self, m: Metrics) -> float:
        cfg = self.cfg
        rise60 = m.rise.get(60, 0.0)
        volx15 = max(m.volx.get(15, 0.0), 1.0)
        above_vwap = m.vwap is not None and m.price > m.vwap
        from_high = m.from_high if m.from_high is not None else 0.0
        dv15 = m.dollar_vol.get(15, 0.0)

        rise_cap = cfg.float("SCORE_RISE_CAP", 50) / 100.0
        volx_cap = cfg.float("SCORE_VOLX_CAP", 2)
        accel_cap = cfg.float("SCORE_ACCEL_CAP", 2)
        fh_cap = cfg.float("SCORE_FROMHIGH_CAP", 15) / 100.0
        dv_cap = cfg.float("SCORE_DOLLAR_CAP", 100000)

        score = 0.0
        score += cfg.float("SCORE_W_RISE", 30) * min(safe_div(rise60, rise_cap, 0.0), 1.0)
        score += cfg.float("SCORE_W_VOLX", 30) * min(
            safe_div(math.log10(volx15), volx_cap, 0.0), 1.0)
        score += cfg.float("SCORE_W_ACCEL", 10) * min(safe_div(m.accel, accel_cap, 0.0), 1.0)
        if above_vwap:
            score += cfg.float("SCORE_W_VWAP", 10)
        score += cfg.float("SCORE_W_FROMHIGH", 10) * max(0.0, 1.0 + safe_div(from_high, fh_cap, 0.0))
        score += cfg.float("SCORE_W_DOLLAR", 10) * min(safe_div(dv15, dv_cap, 0.0), 1.0)
        return clamp(score, 0.0, 100.0)

    def phase_for(self, m: Metrics) -> str:
        fh = m.from_high if m.from_high is not None else 0.0
        if fh <= -0.15:
            return PHASE_FADING
        if m.pct_vs_close is not None and m.pct_vs_close * 100.0 >= self.cfg.float("EXTENDED_PCT", 100):
            return PHASE_EXTENDED
        return PHASE_BUILDING

    # -- ranking ------------------------------------------------------------
    def rank(self, metrics: list[Metrics], top_n: int = 10) -> dict:
        """Champions and hidden gems (6.5)."""
        ranked_by_pct = sorted(
            [m for m in metrics if m.pct_vs_close is not None],
            key=lambda m: m.pct_vs_close, reverse=True)
        pct_rank = {m.symbol: i + 1 for i, m in enumerate(ranked_by_pct)}

        champions = sorted(
            [m for m in metrics if m.tier >= TIER_EARLY or m.score >= 40],
            key=lambda m: m.score, reverse=True)[:5]

        gems = sorted(
            [m for m in metrics if m.tier >= TIER_WATCH
             and pct_rank.get(m.symbol, 9999) > top_n],
            key=lambda m: m.score, reverse=True)[:5]

        return {
            "champions": champions,
            "hidden_gems": gems,
            "pct_rank": pct_rank,
            "by_score": sorted(metrics, key=lambda m: m.score, reverse=True),
        }

    def cycle(self) -> None:
        self.cycles += 1

    # -- momentum builds (surfaced below the alert bar) ---------------------
    def build_candidates(self, metrics: list[Metrics], *,
                         exclude: Optional[set[str]] = None) -> list["BuildSignal"]:
        """Quiet accumulators: ramping volume/price that has not reached tier 2 yet.

        The alert bar is tier `TIER_EARLY` (2). Stocks that are clearly building
        volume and price but sit at `TIER_WATCH` (or just under it) are the ones a
        gainers screen shows too late and the alert rules stay silent on. This
        returns them, ranked, so the operator can watch them before the burst.
        """
        exclude = exclude or set()
        cfg = self.cfg
        min_score = cfg.float("BUILD_MIN_SCORE", 35)
        out: list[BuildSignal] = []
        for m in metrics:
            if m.symbol in exclude or m.tier >= TIER_EARLY:
                continue
            if m.phase in (PHASE_EXTENDED, PHASE_FADING):
                continue
            rise15 = m.rise.get(15, 0.0) or 0.0
            rise60 = m.rise.get(60, 0.0) or 0.0
            volx15 = m.volx.get(15, 0.0) or 0.0
            dv15 = m.dollar_vol.get(15, 0.0) or 0.0
            above_vwap = m.vwap is not None and m.price > m.vwap
            if rise15 < cfg.float("BUILD_RISE15", 1.5) / 100.0:
                continue
            if volx15 < cfg.float("BUILD_VOLX15", 2.0):
                continue
            if dv15 < cfg.float("BUILD_DOLLAR15", 5000):
                continue
            if self.states.get(m.symbol, None) is not None:
                st = self.states[m.symbol]
                if st.history_minutes() < cfg.float("BUILD_MIN_MINUTES", 10):
                    continue

            # How far each tier-2 gate has come (0..1). The weakest gate sets the
            # pace, and the gaps are what the message reports.
            t2_rise = cfg.float("TIER2_RISE60", 8) / 100.0
            t2_rise15 = cfg.float("TIER2_RISE15", 5) / 100.0
            t2_volx = cfg.float("TIER2_VOLX15", 10)
            t2_dv = cfg.float("TIER2_DOLLAR15", 10000)
            rise_prog = min(max(rise60, rise15) / max(t2_rise, t2_rise15, 1e-9), 1.0)
            volx_prog = min(volx15 / t2_volx, 1.0) if t2_volx else 0.0
            dv_prog = min(dv15 / t2_dv, 1.0) if t2_dv else 0.0
            score = 100.0 * (0.4 * rise_prog + 0.35 * volx_prog + 0.15 * dv_prog
                             + (0.1 if above_vwap else 0.0))
            if score < min_score:
                continue

            missing: list[str] = []
            if rise60 < t2_rise and rise15 < t2_rise15:
                missing.append(f"needs +{t2_rise15 * 100:.0f}% 15m or "
                               f"+{t2_rise * 100:.0f}% 60m")
            if volx15 < t2_volx:
                missing.append(f"volume {volx15:.1f}x -> {t2_volx:.0f}x")
            if dv15 < t2_dv:
                missing.append(f"${compact(dv15)} -> ${compact(t2_dv)}")
            if not above_vwap:
                missing.append("reclaim VWAP")

            stage = self._build_stage(score, m, above_vwap, len(missing))
            out.append(BuildSignal(metrics=m, score=round(score, 1), stage=stage,
                                   missing=missing, above_vwap=above_vwap))
        out.sort(key=lambda b: b.score, reverse=True)
        return out

    @staticmethod
    def _build_stage(score: float, m: Metrics, above_vwap: bool, missing_count: int) -> str:
        accel = m.accel or 1.0
        # "Coiled" means it is nearly at the alert bar, not just high on the gates.
        if score >= 70 and missing_count <= 1:
            return "COILED"
        if accel >= 1.5 and above_vwap:
            return "ACCELERATING"
        if above_vwap:
            return "BUILDING"
        return "WARMING"