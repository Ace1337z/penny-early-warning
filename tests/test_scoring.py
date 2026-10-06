"""Scoring: windows, tiers, phase and score."""

from __future__ import annotations

import time

from penny.scoring import (PHASE_FADING, TIER_CONFIRMED, TIER_EARLY, TIER_NONE, TIER_WATCH,
                           Scorer)


def _feed(scorer, symbol, prices, volumes, start, prev_close=1.0, avg=96_000):
    scorer.set_avg_volume(symbol, avg)
    m = None
    cum = 0.0
    for i, price in enumerate(prices):
        cum += volumes[i]
        m = scorer.compute({"symbol": symbol, "ts": start + i * 60, "price": price,
                            "prev_close": prev_close, "cum_volume": cum,
                            "bid": price * 0.999, "ask": price * 1.001, "halted": False},
                           "regular")
    return m


def test_rising_stock_reaches_confirmed(cfg):
    cfg.set("WARMUP_CYCLES", "0")
    scorer = Scorer(cfg)
    scorer.cycles = 5
    prices = [1.0 if i < 10 else 1.0 + (i - 10) * 0.011 for i in range(90)]
    m = _feed(scorer, "TEST", prices, [2500] * 90, time.time() - 3600)
    assert m.tier == TIER_CONFIRMED
    assert 0 <= m.score <= 100
    assert m.vwap is not None and m.price > m.vwap


def test_quiet_noise_does_not_alert(cfg):
    cfg.set("WARMUP_CYCLES", "0")
    scorer = Scorer(cfg)
    scorer.cycles = 5
    prices = [2.0 + (0.01 if i % 2 else -0.01) for i in range(90)]
    m = _feed(scorer, "NOISE", prices, [100] * 90, time.time() - 3600, prev_close=2.0,
              avg=96_000)
    assert m.tier == TIER_NONE


def test_watch_tier_on_mild_move(cfg):
    cfg.set("WARMUP_CYCLES", "0")
    scorer = Scorer(cfg)
    scorer.cycles = 5
    prices = [1.0] * 20 + [1.0 + i * 0.004 for i in range(1, 40)]
    m = _feed(scorer, "WATCH", prices, [700] * 59, time.time() - 3600)
    assert m.tier >= TIER_WATCH


def test_fading_spike_is_tagged(cfg):
    cfg.set("WARMUP_CYCLES", "0")
    scorer = Scorer(cfg)
    scorer.cycles = 5
    prices = [1.0 + i * 0.01 for i in range(60)]
    prices += [max(1.05, 1.60 - (i - 60) * 0.01) for i in range(60, 120)]
    m = _feed(scorer, "FADE", prices, [4000] * 120, time.time() - 7200)
    assert m.phase == PHASE_FADING


def test_warmup_blocks_until_cycles_exceed(cfg):
    cfg.set("WARMUP_CYCLES", "3")
    scorer = Scorer(cfg)
    assert scorer.warm is False
    scorer.cycles = 3
    assert scorer.warm is False
    scorer.cycles = 4
    assert scorer.warm is True


def test_prune_uses_injected_clock(cfg):
    scorer = Scorer(cfg, clock=lambda: 10_000.0)
    st = scorer.state("OLD")
    st.last_seen = 1_000.0
    assert scorer.prune(max_age_seconds=3600) == 1
    assert "OLD" not in scorer.states


def test_ranking_champions_and_gems(cfg):
    from penny.scoring import Metrics
    metrics = []
    for i in range(12):
        m = Metrics(symbol=f"S{i:02}", ts=0.0, price=1.0 + i * 0.1, prev_close=1.0)
        m.score = 50 - i
        m.tier = 2 if i < 3 else 1
        m.pct_vs_close = (10 - i) / 100
        metrics.append(m)
    ranked = Scorer(cfg).rank(metrics)
    assert ranked["champions"]
    assert all(m.tier >= 1 for m in ranked["hidden_gems"])


# --- momentum builds (below the alert bar) ---------------------------------

def _build_feed(scorer, symbol="BUILD", samples=90, per_sample=400.0,
                step=0.00233, avg=96_000):
    prices = [1.0 + i * step for i in range(samples)]
    return _feed(scorer, symbol, prices, [per_sample] * samples, time.time() - 3600,
                 prev_close=1.0, avg=avg)


def test_build_candidate_is_surfaced_below_the_alert_bar(cfg):
    cfg.set("WARMUP_CYCLES", "0")
    scorer = Scorer(cfg)
    scorer.cycles = 5
    m = _build_feed(scorer)
    # It is a genuine build, but not yet an alert.
    assert m.tier < TIER_EARLY
    assert m.rise.get(15, 0) >= 0.015
    builds = scorer.build_candidates([m])
    assert len(builds) == 1
    sig = builds[0]
    assert sig.metrics.symbol == "BUILD"
    assert 0 < sig.score <= 100
    assert sig.stage in ("WARMING", "BUILDING", "ACCELERATING", "COILED")
    assert sig.missing  # it names what is still needed


def test_build_candidate_excludes_quiet_noise(cfg):
    cfg.set("WARMUP_CYCLES", "0")
    scorer = Scorer(cfg)
    scorer.cycles = 5
    m = _feed(scorer, "NOISE", [2.0 + (0.01 if i % 2 else -0.01) for i in range(90)],
              [100] * 90, time.time() - 3600, prev_close=2.0, avg=96_000)
    assert scorer.build_candidates([m]) == []


def test_build_candidate_excludes_alert_tier_stocks(cfg):
    cfg.set("WARMUP_CYCLES", "0")
    scorer = Scorer(cfg)
    scorer.cycles = 5
    m = _feed(scorer, "TEST", [1.0 if i < 10 else 1.0 + (i - 10) * 0.011
                               for i in range(90)], [2500] * 90,
              time.time() - 3600)
    assert m.tier >= TIER_EARLY
    assert scorer.build_candidates([m]) == []


def test_build_candidate_excludes_fading_stocks(cfg):
    cfg.set("WARMUP_CYCLES", "0")
    scorer = Scorer(cfg)
    scorer.cycles = 5
    m = _build_feed(scorer, "FADE")
    m.phase = PHASE_FADING
    assert scorer.build_candidates([m]) == []


def test_build_candidate_honours_a_minimum_score(cfg):
    cfg.set("WARMUP_CYCLES", "0")
    cfg.set("BUILD_MIN_SCORE", "101")  # impossible
    scorer = Scorer(cfg)
    scorer.cycles = 5
    m = _build_feed(scorer)
    assert scorer.build_candidates([m]) == []


def test_build_candidate_ranking_is_by_score(cfg):
    cfg.set("WARMUP_CYCLES", "0")
    scorer = Scorer(cfg)
    scorer.cycles = 5
    weak = _build_feed(scorer, "WEAK", per_sample=400.0, step=0.0016)
    strong = _build_feed(scorer, "STRONG", per_sample=900.0, step=0.003)
    builds = scorer.build_candidates([weak, strong])
    assert [b.metrics.symbol for b in builds] == ["STRONG", "WEAK"]


def test_build_stage_needs_near_completion_to_coil(cfg):
    from penny.scoring import Metrics as _M
    scorer = Scorer(cfg)
    m = _M(symbol="X", ts=0.0, price=1.0)
    m.accel = 1.0
    # High progress but two gates still open is not "coiled".
    assert scorer._build_stage(74.0, m, True, missing_count=2) == "BUILDING"
    # High progress with one gate left is.
    assert scorer._build_stage(96.0, m, True, missing_count=1) == "COILED"
    # A fast, above-VWAP builder that is not near the bar.
    assert scorer._build_stage(50.0, m, True, missing_count=3) == "BUILDING"
    m.accel = 2.0
    assert scorer._build_stage(50.0, m, True, missing_count=3) == "ACCELERATING"
    assert scorer._build_stage(50.0, m, False, missing_count=3) == "WARMING"
