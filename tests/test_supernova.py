"""Supernova phase classifier + ARMED->ENTER confirmation (Spec 2.3, 2.4, 3)."""

from __future__ import annotations

from penny.strategies import confirmation as conf
from penny.strategies import supernova as sn
from penny.strategies.base import Bundle, StrategyGate, StrategyResult


def bundle(**kw) -> Bundle:
    base = dict(symbol="TEST", price=1.0, day_high=1.0, from_high=0.0,
                accel=1.0, volx={3: 1.0}, pct_vs_close=0.0)
    base.update(kw)
    return Bundle(**base)


# --- phase classifier: exactly one phase, always ----------------------------


def test_classifier_always_returns_exactly_one_known_phase():
    # A spread of wildly different inputs; every one must land on one phase.
    cases = [
        bundle(price=1.0, day_high=1.0, from_high=0.0),
        bundle(price=1.0, day_high=2.0, from_high=-0.5),
        bundle(price=5.0, day_high=5.0, from_high=0.0, accel=3.0, volx={3: 8.0}),
        bundle(price=1.0, day_high=1.0, from_high=0.0, pct_vs_close=2.5, accel=0.5,
               volx={3: 0.4}),
        Bundle(symbol="EMPTY"),
    ]
    for b in cases:
        phase = sn.classify_bundle(b)
        assert phase.phase in sn.PHASES


def test_parabolic_near_high_on_accelerating_volume():
    phase = sn.classify(price=10.0, day_high=10.0, from_high=0.0, accel=2.0,
                        volx_3m=6.0, above_vwap=True)
    assert phase.phase == sn.PARABOLIC


def test_front_side_near_high_on_confirming_volume():
    phase = sn.classify(price=10.0, day_high=10.1, from_high=-0.01, accel=1.1,
                        volx_3m=2.5, above_vwap=True)
    assert phase.phase == sn.FRONT_SIDE


def test_blow_off_crash_off_high_below_vwap():
    phase = sn.classify(price=9.0, day_high=10.0, from_high=-0.10, accel=0.5,
                        volx_3m=4.0, above_vwap=False)
    assert phase.phase == sn.BLOW_OFF_CRASH
    assert phase.dangerous


def test_back_side_off_high_not_crashed():
    # -4% off the high with no confirming volume: cannot prove front side, so
    # the safe read is PRE_BREAKOUT (no chase).
    phase = sn.classify(price=9.6, day_high=10.0, from_high=-0.04, accel=0.9,
                        volx_3m=1.5, above_vwap=True)
    assert phase.phase == sn.PRE_BREAKOUT
    # -8% off the high, decelerating, but still above VWAP: the back side
    # (a crash would be below VWAP).
    phase2 = sn.classify(price=9.2, day_high=10.0, from_high=-0.08, accel=0.9,
                         volx_3m=1.5, above_vwap=True)
    assert phase2.phase == sn.BACK_SIDE
    assert phase2.dangerous


def test_former_supernova_big_gain_mostly_given_back():
    phase = sn.classify(price=2.0, day_high=4.0, from_high=-0.50, accel=0.7,
                        volx_3m=1.0, pct_vs_close=1.2, above_vwap=False)
    assert phase.phase == sn.FORMER_SUPERNOVA
    assert phase.dangerous


# --- telemetry guard (Spec 2.4) ---------------------------------------------


def test_guard_off_high_but_accelerating_is_parabolic_not_back_side():
    # The conflict: 8% off the high would read BACK_SIDE, but the tape is
    # accelerating -- the guard resolves it to PARABOLIC and flags the fix.
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


# --- trade class: CONTINUATION xor REVERSAL ---------------------------------


def test_trade_class_from_trigger():
    g = StrategyGate(passed=True, entry_trigger="v_reversal", primary="vwap_reclaim")
    assert conf.trade_class_for(g) == conf.REVERSAL
    g2 = StrategyGate(passed=True, entry_trigger="mss_break", primary="vwap_reclaim")
    assert conf.trade_class_for(g2) == conf.CONTINUATION


def test_trade_class_from_primary_when_no_trigger():
    g = StrategyGate(passed=True, primary="liquidity_grab")
    assert conf.trade_class_for(g) == conf.REVERSAL
    g2 = StrategyGate(passed=True, primary="dollar_breakout")
    assert conf.trade_class_for(g2) == conf.CONTINUATION


def test_trade_class_is_always_one_of_two():
    for primary in (None, "a", "v_reversal", "liquidity_grab", "dollar_breakout"):
        for trigger in (None, "v_reversal", "mss_break"):
            g = StrategyGate(passed=True, primary=primary, entry_trigger=trigger)
            assert conf.trade_class_for(g) in (conf.CONTINUATION, conf.REVERSAL)


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
    assert setup.trade_class == conf.CONTINUATION


def test_arm_refuses_without_price():
    g = StrategyGate(passed=True)
    assert conf.arm(bundle(price=0.0, day_high=0.0), g, phase=sn.FRONT_SIDE) is None


# --- confirmation (Stage 2) -------------------------------------------------


def armed(price=1.05, trigger=1.05, ts=1000.0, trade_class=conf.CONTINUATION):
    return conf.ArmedState(symbol="TEST", ts=ts, price=price, trigger=trigger,
                           phase=sn.FRONT_SIDE, trade_class=trade_class)


def bars(*rows):
    # each row: (o, h, l, c, v)
    return [{"t": 1000 + i * 60, "o": o, "h": h, "l": l, "c": c, "v": v}
            for i, (o, h, l, c, v) in enumerate(rows)]


def test_confirm_primitive_a_break_and_hold():
    # Two closed bars; the last closed above the trigger.
    b = bundle(price=1.06, bars_1m=bars((1.02, 1.04, 1.01, 1.03, 100),
                                        (1.03, 1.09, 1.05, 1.08, 200),
                                        (1.08, 1.10, 1.07, 1.09, 150)))  # forming
    res = conf.confirm(armed(), b, now_ts=1100.0)
    assert res.state == "ENTER"
    assert res.primitive in ("A", "C")


def test_confirm_primitive_b_reclaim():
    # Wicked under the trigger, opened under, closed back above.
    b = bundle(price=1.06, bars_1m=bars((1.06, 1.07, 1.05, 1.06, 100),
                                        (1.04, 1.08, 1.00, 1.07, 150),   # closed bar
                                        (1.07, 1.08, 1.06, 1.07, 120)))  # forming
    res = conf.confirm(armed(), b, now_ts=1100.0)
    assert res.state == "ENTER"
    assert res.primitive in ("B", "C")


def test_confirm_stays_armed_without_a_close_above():
    b = bundle(price=1.01, bars_1m=bars((1.00, 1.02, 0.98, 1.01, 100),
                                        (1.01, 1.03, 1.00, 1.02, 100),
                                        (1.02, 1.03, 1.01, 1.02, 90)))
    res = conf.confirm(armed(), b, now_ts=1100.0)
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


def test_confirm_within_chase_cap_enters():
    # +3.9% above armed price stays inside the 4% cap.
    b = bundle(price=1.039, bars_1m=bars((1.00, 1.02, 0.99, 1.01, 100),
                                         (1.01, 1.08, 1.00, 1.07, 200),
                                         (1.07, 1.08, 1.06, 1.07, 150)))
    res = conf.confirm(armed(price=1.00), b, now_ts=1100.0)
    assert res.state == "ENTER"


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
    assert b.accel is not None


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
    # Stage 1: a fresh setup is armed, not fired.
    b1 = bundle(price=1.00, day_high=1.00, from_high=0.0,
                supernova_phase=sn.FRONT_SIDE, volx={3: 3.0})
    hold = engine._armed_decision(_metrics("ABCD"), gate, b1)
    assert hold is not None and "armed" in hold
    assert "ABCD" in engine._armed

    # Stage 2: a closed bar still below the trigger keeps it armed.
    b2 = bundle(price=1.01, day_high=1.00, from_high=0.0,
                supernova_phase=sn.FRONT_SIDE,
                bars_1m=bars((1.00, 1.01, 0.99, 1.00, 100),
                             (1.00, 1.01, 0.99, 1.00, 100)))
    hold2 = engine._armed_decision(_metrics("ABCD"), gate, b2)
    assert hold2 is not None

    # Stage 2: a closed bar above the trigger confirms -- fires (None).
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
