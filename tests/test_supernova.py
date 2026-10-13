"""Supernova phase classifier + ARMED->ENTER confirmation (Spec 2.3, 2.4, 3)."""

from __future__ import annotations

from penny.strategies import confirmation as conf
from penny.strategies import supernova as sn
from penny.strategies.base import Bundle, StrategyGate
from penny.strategies.indicators import (ema, expected_cum_fraction,
                                         float_turnover, overhead_levels,
                                         range_position, tod_rvol)


def bundle(**kw) -> Bundle:
    base = dict(symbol="TEST", price=1.0, day_high=1.0, from_high=0.0,
                accel=1.0, volx={3: 1.0}, pct_vs_close=0.0)
    base.update(kw)
    return Bundle(**base)


# --- phase classifier: exactly one phase, always ----------------------------


def test_classifier_always_returns_exactly_one_known_phase():
    cases = [
        bundle(price=1.0, day_high=1.0, from_high=0.0),
        bundle(price=1.0, day_high=2.0, from_high=-0.5),
        bundle(price=5.0, day_high=5.0, from_high=0.0, accel=3.0, volx={3: 8.0}),
        bundle(price=1.0, day_high=1.0, from_high=0.0, pct_vs_close=2.5, accel=0.5,
               volx={3: 0.4}),
        Bundle(symbol="EMPTY"),
    ]
    for b in cases:
        assert sn.classify_bundle(b).phase in sn.PHASES


def test_parabolic_on_ema9_extension():
    # >35% above the 1m EMA9 is the vertical-extension test.
    phase = sn.classify(price=1.40, day_high=1.40, from_high=0.0, accel=2.0,
                        volx_3m=6.0, ema9=1.00, above_vwap=True)
    assert phase.phase == sn.PARABOLIC
    assert phase.ema9_ext_pct and phase.ema9_ext_pct > 35


def test_parabolic_on_vwap_extension():
    # >45% above VWAP also qualifies even if the EMA9 is not available.
    phase = sn.classify(price=1.50, day_high=1.50, from_high=0.0, accel=2.0,
                        volx_3m=6.0, vwap=1.00, above_vwap=True)
    assert phase.phase == sn.PARABOLIC
    assert phase.vwap_ext_pct and phase.vwap_ext_pct > 45


def test_front_side_above_vwap_on_confirming_volume():
    phase = sn.classify(price=10.0, day_high=10.1, from_high=-0.01, accel=1.1,
                        volx_3m=2.5, vwap=9.8, above_vwap=True)
    assert phase.phase == sn.FRONT_SIDE


def test_blow_off_crash_off_high_below_vwap():
    phase = sn.classify(price=9.0, day_high=10.0, from_high=-0.10, accel=0.5,
                        volx_3m=4.0, above_vwap=False)
    assert phase.phase == sn.BLOW_OFF_CRASH
    assert phase.dangerous


def test_back_side_off_high_not_crashed():
    # -16% off the high, no crash (still above VWAP): the back side.
    phase = sn.classify(price=8.4, day_high=10.0, from_high=-0.16, accel=0.9,
                        volx_3m=1.5, above_vwap=True)
    assert phase.phase == sn.BACK_SIDE
    assert phase.dangerous


def test_former_supernova_big_gain_mostly_given_back():
    phase = sn.classify(price=2.0, day_high=4.0, from_high=-0.50, accel=0.7,
                        volx_3m=1.0, pct_vs_close=1.2, above_vwap=False)
    assert phase.phase == sn.FORMER_SUPERNOVA
    assert phase.dangerous


# --- telemetry guard (Spec 2.4) ---------------------------------------------


def test_guard_off_high_but_accelerating_is_parabolic_not_back_side():
    # 8% off the high would read BLOW_OFF, but the tape is accelerating -- the
    # guard resolves it to PARABOLIC and flags the correction.
    phase = sn.classify(price=9.2, day_high=10.0, from_high=-0.08, accel=2.0,
                        volx_3m=5.0, above_vwap=True)
    assert phase.phase == sn.PARABOLIC
    assert phase.guard_fired
    assert not phase.dangerous


def test_guard_label_never_accelerating_on_back_side():
    label, corrected = sn.telemetry_guard(sn.BACK_SIDE, 2.0)
    assert label == "fading"
    assert corrected is True


def test_guard_label_passes_through_healthy_phases():
    label, corrected = sn.telemetry_guard(sn.FRONT_SIDE, 2.0)
    assert label == "accelerating"
    assert corrected is False


# --- mode: CONTINUATION xor REVERSAL ----------------------------------------


def test_mode_from_trigger():
    assert conf.mode_for("v_reversal") == conf.REVERSAL
    assert conf.mode_for("mss_break") == conf.CONTINUATION
    assert conf.mode_for("gap_and_go") == conf.CONTINUATION


def test_mode_from_primary_when_no_trigger():
    assert conf.mode_for(None, "v_reversal") == conf.REVERSAL
    assert conf.mode_for(None, "dollar_breakout") == conf.CONTINUATION


def test_mode_for_name_routes_the_spec_sets():
    # Spec 3.2: reversal names are reversal, everything else continuation.
    assert conf.mode_for_name("v_reversal") == conf.REVERSAL
    assert conf.mode_for_name("morning_panic_dip_buy") == conf.REVERSAL
    assert conf.mode_for_name("liquidity_grab") == conf.REVERSAL
    assert conf.mode_for_name("first_pullback") == conf.CONTINUATION
    assert conf.mode_for_name("mss_break") == conf.CONTINUATION
    assert conf.mode_for_name(None) == conf.CONTINUATION


def test_trigger_or_name_prefers_the_trigger():
    # The trigger names the entry mechanic when one fired.
    assert conf.mode_for_trigger_or_name("mss_break", "v_reversal") == conf.CONTINUATION
    assert conf.mode_for_trigger_or_name("v_reversal", "dollar_breakout") == conf.REVERSAL
    assert conf.mode_for_trigger_or_name(None, "v_reversal") == conf.REVERSAL


def test_gate_records_a_confirmation_mode():
    from penny.strategies.gate import evaluate_gate
    b = bundle(price=1.10, day_high=1.10, from_high=0.0, vwap=1.05,
               volx={3: 3.0, 5: 3.0}, rise={3: 0.05, 5: 0.05}, pct_vs_close=0.10,
               news=[{"title": "Acme awarded $12M contract", "source": "Benzinga",
                      "url": "", "age": "1h"}])
    gate = evaluate_gate(b)
    assert gate.confirmation_mode in (conf.CONTINUATION, conf.REVERSAL)
    assert gate.as_dict()["confirmation_mode"] == gate.confirmation_mode


def test_morning_panic_dip_buy_qualifies_a_flush():
    from penny.strategies.detectors import morning_panic_dip_buy
    b = bundle(price=0.84, day_high=1.05, from_high=-0.20, prev_close=1.00,
               minutes_since_low=30.0, volx={3: 3.0, 5: 3.0})
    res = morning_panic_dip_buy(b)
    assert res.verdict == "PASS"
    assert conf.mode_for_name("morning_panic_dip_buy") == conf.REVERSAL


def test_morning_panic_dip_buy_refuses_a_small_drop():
    from penny.strategies.detectors import morning_panic_dip_buy
    b = bundle(price=0.98, day_high=1.00, from_high=-0.02, prev_close=1.00,
               minutes_since_low=20.0, volx={3: 3.0, 5: 3.0})
    assert morning_panic_dip_buy(b).verdict == "FAIL"


def test_mode_is_always_one_of_two():
    for primary in (None, "a", "v_reversal", "dollar_breakout"):
        for trigger in (None, "v_reversal", "mss_break", "gap_and_go"):
            assert conf.mode_for(trigger, primary) in (conf.CONTINUATION, conf.REVERSAL)


# --- arming (Stage 1) -------------------------------------------------------


def test_arm_only_from_actionable_phases():
    g = StrategyGate(passed=True, entry_trigger="mss_break")
    b = bundle(price=1.0, day_high=1.0)
    assert conf.arm(b, g, phase=sn.FRONT_SIDE) is not None
    assert conf.arm(b, g, phase=sn.PRE_BREAKOUT) is not None
    assert conf.arm(b, g, phase=sn.PARABOLIC) is None
    assert conf.arm(b, g, phase=sn.BACK_SIDE) is None
    assert conf.arm(b, g, phase=sn.FORMER_SUPERNOVA) is None


def test_arm_trigger_is_just_above_the_high():
    g = StrategyGate(passed=True, entry_trigger="mss_break")
    setup = conf.arm(bundle(price=0.9, day_high=1.0), g, phase=sn.FRONT_SIDE)
    assert setup.trigger > 1.0
    assert setup.mode == conf.CONTINUATION


def test_arm_refuses_without_price():
    g = StrategyGate(passed=True)
    assert conf.arm(bundle(price=0.0, day_high=0.0), g, phase=sn.FRONT_SIDE) is None


# --- confirmation (Stage 2) -------------------------------------------------


def armed(price=1.05, trigger=1.05, ts=1000.0, mode=conf.CONTINUATION, hod_ts=None):
    return conf.ArmedState(symbol="TEST", ts=ts, price=price, trigger=trigger,
                           phase=sn.FRONT_SIDE, mode=mode, hod_ts=hod_ts)


def bars(*rows):
    # each row: (o, h, l, c, v)
    return [{"t": 1000 + i * 60, "o": o, "h": h, "l": l, "c": c, "v": v}
            for i, (o, h, l, c, v) in enumerate(rows)]


def test_confirm_primitive_a_volume_reversal_bar():
    # Last closed bar: green, close in upper half, volume >= 2x the 5-bar avg.
    b = bundle(price=1.06, bars_1m=bars((1.00, 1.02, 0.99, 1.01, 100),
                                        (1.01, 1.03, 1.00, 1.02, 100),
                                        (1.02, 1.04, 1.01, 1.03, 100),
                                        (1.03, 1.05, 1.02, 1.04, 100),
                                        (1.04, 1.08, 1.03, 1.07, 400),
                                        (1.07, 1.09, 1.06, 1.08, 120)))  # forming
    res = conf.confirm(armed(), b, now_ts=1100.0)
    assert res.state == "ENTER"
    assert res.primitive == "A"


def test_confirm_primitive_c_trigger_break():
    # No volume spike, but a close above the trigger: the break.
    b = bundle(price=1.06, bars_1m=bars((1.00, 1.02, 0.99, 1.01, 100),
                                        (1.01, 1.06, 1.00, 1.055, 100),
                                        (1.055, 1.07, 1.05, 1.065, 100)))
    res = conf.confirm(armed(trigger=1.05), b, now_ts=1100.0)
    assert res.state == "ENTER"
    assert res.primitive in ("A", "C")


def test_confirm_primitive_b_higher_low():
    # No close above the trigger, but a higher low and a green bar.
    b = bundle(price=1.02, bars_1m=bars((1.00, 1.02, 0.98, 1.00, 100),
                                        (1.00, 1.03, 0.99, 1.02, 100),
                                        (1.02, 1.03, 1.01, 1.02, 90)))
    res = conf.confirm(armed(trigger=1.05), b, now_ts=1100.0)
    assert res.state == "ENTER"
    assert res.primitive == "B"


def test_confirm_stays_armed_without_a_close_above():
    # Last CLOSED bar (the one before the forming bar): lower low, red, no
    # volume spike, no trigger break.
    b = bundle(price=1.01, bars_1m=bars((1.00, 1.02, 0.98, 1.01, 100),
                                        (1.01, 1.03, 1.00, 1.02, 100),
                                        (1.02, 1.025, 0.99, 1.00, 90),
                                        (1.00, 1.01, 0.99, 1.005, 50)))  # forming
    res = conf.confirm(armed(trigger=1.20), b, now_ts=1100.0)
    assert res.state == "ARMED"


def test_confirm_stays_armed_with_no_closed_bar():
    b = bundle(price=1.01, bars_1m=bars((1.00, 1.02, 0.98, 1.01, 100)))
    res = conf.confirm(armed(), b, now_ts=1100.0)
    assert res.state == "ARMED"
    assert "closed" in res.reasons[0]


def test_confirm_no_trade_when_chase_exceeds_cap():
    b = bundle(price=1.10, bars_1m=bars((1.00, 1.02, 0.98, 1.01, 100),
                                        (1.01, 1.12, 1.00, 1.11, 100),
                                        (1.11, 1.12, 1.10, 1.11, 90)))
    res = conf.confirm(armed(price=1.00), b, now_ts=1100.0)
    assert res.state == "NO_TRADE"
    assert "chase" in res.reasons[0]


def test_confirm_no_trade_after_ttl():
    b = bundle(price=1.01, bars_1m=bars((1.00, 1.02, 0.98, 1.01, 100),
                                        (1.01, 1.02, 1.00, 1.01, 100)))
    res = conf.confirm(armed(ts=1000.0), b, now_ts=2000.0, ttl_s=600.0)
    assert res.state == "NO_TRADE"
    assert "expired" in res.reasons[0]


def test_confirm_reject_without_armed_setup():
    assert conf.confirm(None, bundle()).state == "REJECT"


# --- continuation hard-veto (Spec 3.2) --------------------------------------


def test_continuation_veto_on_back_side():
    b = bundle(price=1.06, supernova_phase=sn.BACK_SIDE)
    res = conf.confirm(armed(), b, now_ts=1100.0)
    assert res.state == "NO_TRADE"
    assert "continuation vetoed" in res.reasons[0]


def test_continuation_veto_on_fast_drop_off_hod():
    # 12% off the HOD, HOD set 60s ago -> the falling-knife veto.
    b = bundle(price=0.88, supernova_phase=sn.FRONT_SIDE, from_high=-0.12,
               bars_1m=bars((1.00, 1.02, 0.98, 1.01, 100),
                            (1.01, 1.02, 1.00, 1.01, 100)))
    res = conf.confirm(armed(price=1.00, ts=1000.0, hod_ts=1040.0), b, now_ts=1100.0)
    assert res.state == "NO_TRADE"
    assert "off HOD" in res.reasons[0]


def test_reversal_mode_requires_flush_then_green():
    # The last two CLOSED bars: a red flush then a green reversal (the final
    # entry is the forming bar). Price is within the 4% chase cap of 1.00.
    b = bundle(price=1.03, supernova_phase=sn.BACK_SIDE,
               bars_1m=bars((1.10, 1.11, 1.09, 1.10, 100),
                            (1.05, 1.06, 0.98, 1.00, 200),   # flush, red (closed)
                            (1.01, 1.07, 1.00, 1.065, 150),  # green (closed)
                            (1.065, 1.07, 1.06, 1.07, 120)))  # forming
    res = conf.confirm(armed(price=1.00, trigger=1.05, mode=conf.REVERSAL), b,
                       now_ts=1100.0)
    assert res.state == "ENTER"


def test_reversal_mode_stays_armed_without_flush():
    # The last two closed bars are both green -- no completed flush: hold.
    b = bundle(price=1.03, supernova_phase=sn.BACK_SIDE,
               bars_1m=bars((1.00, 1.02, 0.98, 1.01, 100),
                            (1.02, 1.08, 1.01, 1.07, 150),   # green (closed)
                            (1.07, 1.08, 1.06, 1.075, 120),  # green (closed)
                            (1.075, 1.08, 1.07, 1.075, 50)))  # forming
    res = conf.confirm(armed(price=1.00, trigger=1.05, mode=conf.REVERSAL), b,
                       now_ts=1100.0)
    assert res.state == "ARMED"


# --- indicators: EMA, TOD-RVOL, turnover, structural levels -----------------


def test_ema_needs_a_full_period():
    assert ema([1, 2, 3], 9) is None
    assert ema([1.0] * 9, 9) == 1.0


def test_range_position():
    assert range_position({"h": 2.0, "l": 1.0, "c": 1.75}) == 0.75
    assert range_position({"h": 1.0, "l": 1.0, "c": 1.0}) is None


def test_tod_rvol_front_loads_the_open():
    # Same cumulative volume is a *bigger* multiple early in the session.
    early = tod_rvol(30_000, 1_000_000, 5)
    late = tod_rvol(30_000, 1_000_000, 300)
    assert early is not None and late is not None
    assert early > late


def test_tod_rvol_none_without_average():
    assert tod_rvol(10_000, None, 60) is None
    assert tod_rvol(10_000, 0, 60) is None


def test_float_turnover():
    assert float_turnover(5_000_000, 1_000_000) == 5.0
    assert float_turnover(5_000_000, None) is None


def test_expected_cum_fraction_is_monotonic():
    xs = [expected_cum_fraction(m) for m in range(1, 400)]
    assert all(b >= a for a, b in zip(xs, xs[1:]))
    assert 0 < xs[0] <= 1 and xs[-1] <= 1.0


def test_overhead_levels_above_price_only_and_sorted():
    lv = overhead_levels(1.03, day_high=1.20, prior_highs=[1.10, 0.90],
                         pivots=[1.50])
    assert lv == sorted(lv)
    assert all(v > 1.03 for v in lv)
    assert 1.20 in lv


# --- phase is wired onto the Bundle -----------------------------------------


def test_bundle_carries_a_supernova_phase_from_the_wiring(tmp_path):
    from penny.scoring import Scorer
    from penny.strategies.wiring import metrics_to_bundle

    cfg = _cfg(tmp_path)
    scorer = Scorer(cfg)
    scorer.set_avg_volume("ABCD", 400_000.0)
    m = None
    now = 1_700_000_000.0
    for i in range(25):
        price = 1.10 + i * 0.02
        out = scorer.compute({
            "symbol": "ABCD", "ts": now + i * 60, "price": price,
            "cum_volume": 200_000 + i * 400_000, "prev_close": 1.09,
            "high_price": price, "float_shares": 5_000_000,
        }, "regular")
        if out is not None:
            m = out
    assert m is not None
    b = metrics_to_bundle(m)
    assert b.supernova_phase in sn.PHASES


# --- engine state machine (flag off = unchanged; flag on = two-step) --------


def test_armed_confirm_flag_defaults_off(tmp_path):
    from penny.engine import Engine
    from penny.store import open_journal, open_state
    from penny.telegram import FakeTelegram
    cfg = _cfg(tmp_path)
    db = open_state(tmp_path / "state.db")
    journal = open_journal(tmp_path / "journal.db")
    engine = Engine(cfg, db, journal, FakeTelegram(cfg))
    assert engine._armed_confirm_enabled() is False


def test_armed_decision_arms_then_holds_then_fires(tmp_path):
    from penny.engine import Engine
    from penny.store import open_journal, open_state
    from penny.telegram import FakeTelegram
    cfg = _cfg(tmp_path)
    cfg.set("ARMED_CONFIRM", "1")
    db = open_state(tmp_path / "state.db")
    journal = open_journal(tmp_path / "journal.db")
    engine = Engine(cfg, db, journal, FakeTelegram(cfg))
    assert engine._armed_confirm_enabled() is True

    gate = StrategyGate(passed=True, entry_trigger="mss_break", primary="vwap_reclaim")
    b1 = bundle(price=1.00, day_high=1.00, from_high=0.0,
                supernova_phase=sn.FRONT_SIDE, volx={3: 3.0})
    hold = engine._armed_decision(_metrics("ABCD"), gate, b1)
    assert hold is not None and "armed" in hold
    assert "ABCD" in engine._armed

    b2 = bundle(price=1.01, day_high=1.00, from_high=0.0,
                supernova_phase=sn.FRONT_SIDE,
                bars_1m=bars((1.00, 1.01, 0.99, 1.00, 100),
                             (1.00, 1.01, 0.99, 1.00, 100)))
    hold2 = engine._armed_decision(_metrics("ABCD"), gate, b2)
    assert hold2 is not None

    trig = engine._armed["ABCD"].trigger
    b3 = bundle(price=trig * 1.01, day_high=1.00, from_high=0.0,
                supernova_phase=sn.FRONT_SIDE,
                bars_1m=bars((1.00, trig, 0.99, trig, 100),
                             (trig, trig * 1.02, trig, trig * 1.01, 200),
                             (trig * 1.01, trig * 1.02, trig, trig * 1.01, 150)))
    fired = engine._armed_decision(_metrics("ABCD"), gate, b3)
    assert fired is None
    assert "ABCD" not in engine._armed


def test_armed_decision_disarms_on_dangerous_phase(tmp_path):
    from penny.engine import Engine
    from penny.store import open_journal, open_state
    from penny.telegram import FakeTelegram
    cfg = _cfg(tmp_path)
    cfg.set("ARMED_CONFIRM", "1")
    db = open_state(tmp_path / "state.db")
    journal = open_journal(tmp_path / "journal.db")
    engine = Engine(cfg, db, journal, FakeTelegram(cfg))
    gate = StrategyGate(passed=True, entry_trigger="mss_break")
    engine._armed_decision(_metrics("ABCD"), gate,
                           bundle(price=1.00, day_high=1.00, from_high=0.0,
                                  supernova_phase=sn.FRONT_SIDE))
    assert "ABCD" in engine._armed
    hold = engine._armed_decision(_metrics("ABCD"), gate,
                                  bundle(price=0.90, day_high=1.00, from_high=-0.10,
                                         supernova_phase=sn.BLOW_OFF_CRASH))
    assert "ABCD" not in engine._armed
    assert "disarmed" in hold


def test_armed_decision_refuses_dangerous_phase_immediately(tmp_path):
    from penny.engine import Engine
    from penny.store import open_journal, open_state
    from penny.telegram import FakeTelegram
    cfg = _cfg(tmp_path)
    cfg.set("ARMED_CONFIRM", "1")
    db = open_state(tmp_path / "state.db")
    journal = open_journal(tmp_path / "journal.db")
    engine = Engine(cfg, db, journal, FakeTelegram(cfg))
    gate = StrategyGate(passed=True, entry_trigger="mss_break")
    hold = engine._armed_decision(_metrics("ABCD"), gate,
                                  bundle(price=0.90, day_high=1.00, from_high=-0.10,
                                         supernova_phase=sn.BACK_SIDE))
    assert hold is not None
    assert "ABCD" not in engine._armed


# -- helpers -----------------------------------------------------------------


def _cfg(tmp_path):
    from penny.config import Config
    return Config(path=tmp_path / "config.env")


def _metrics(symbol="ABCD"):
    from penny.scoring import Metrics
    return Metrics(symbol=symbol, ts=1_700_000_000.0, price=1.0)


# --- catalyst-aware routing (Spec 3.3) --------------------------------------


def test_catalyst_tier_company_event_is_tier_1():
    from penny.strategies.routing import TIER_1, catalyst_tier
    ranked = [{"class": "HARD", "headline": "Acme awarded $12M contract"}]
    assert catalyst_tier(bundle(), ranked) == TIER_1


def test_catalyst_tier_sector_is_tier_2():
    from penny.strategies.routing import TIER_2, catalyst_tier
    ranked = [{"class": "MEDIUM", "headline": "sector rally lifts small caps"}]
    assert catalyst_tier(bundle(), ranked) == TIER_2


def test_catalyst_tier_no_news_is_tier_3():
    from penny.strategies.routing import TIER_3, catalyst_tier
    assert catalyst_tier(bundle(), []) == TIER_3


def test_size_multiplier_by_tier():
    from penny.strategies import routing as r
    assert r.size_multiplier(r.TIER_1) == 1.0
    assert r.size_multiplier(r.TIER_2) == 0.75
    assert r.size_multiplier(r.TIER_3) == 0.5


def test_tier3_continuation_vetoed_after_1030_et():
    from datetime import datetime
    from penny.strategies import routing as r
    late = datetime(2026, 1, 2, 11, 0)
    early = datetime(2026, 1, 2, 10, 0)
    assert r.continuation_allowed(r.TIER_3, late, "CONTINUATION") is False
    assert r.continuation_allowed(r.TIER_3, early, "CONTINUATION") is True
    # Reversal and Tier 1/2 continuations are unaffected at any hour.
    assert r.continuation_allowed(r.TIER_3, late, "REVERSAL") is True
    assert r.continuation_allowed(r.TIER_1, late, "CONTINUATION") is True


def test_tier3_continuation_veto_in_confirmation():
    from datetime import datetime
    b = bundle(price=1.05, supernova_phase=sn.FRONT_SIDE, from_high=-0.01,
               catalyst_tier=3, now_et=datetime(2026, 1, 2, 11, 0),
               bars_1m=bars((1.00, 1.06, 1.00, 1.05, 100),
                            (1.05, 1.07, 1.04, 1.06, 100)))
    res = conf.confirm(armed(), b, now_ts=1100.0)
    assert res.state == "NO_TRADE"
    assert "Tier 3" in res.reasons[0]


def test_catalyst_tier_line_renders():
    from penny.alerts import catalyst_tier_line
    assert catalyst_tier_line(bundle(catalyst_tier=1, size_multiplier=1.0)) == "TIER 1 -- full size"
    assert catalyst_tier_line(bundle(catalyst_tier=3, size_multiplier=0.5)) == "TIER 3 -- 0.50x size"
    assert catalyst_tier_line(None) is None
