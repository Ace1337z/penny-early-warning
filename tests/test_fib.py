"""Fibonacci levels and trade plans."""

from __future__ import annotations

import pytest

from penny.fib import fib_levels, trade_plan


def test_retracements():
    levels = fib_levels(1.00, 2.00)
    assert abs(levels.retracements["23.6"] - 1.764) < 1e-6
    assert abs(levels.retracements["38.2"] - 1.618) < 1e-6
    assert abs(levels.retracements["50.0"] - 1.500) < 1e-6
    assert abs(levels.retracements["61.8"] - 1.382) < 1e-6
    assert abs(levels.retracements["78.6"] - 1.214) < 1e-6


def test_extensions():
    levels = fib_levels(1.00, 2.00)
    assert abs(levels.extensions["127.2"] - 2.272) < 1e-6
    assert abs(levels.extensions["161.8"] - 2.618) < 1e-6


def test_breakout_plan_near_high():
    levels = fib_levels(1.00, 2.00)
    plan = trade_plan(1.95, levels, risk_usd=50)
    assert plan is not None
    assert plan.mode == "breakout"
    assert plan.shares >= 0
    assert plan.stop < plan.entry


def test_pullback_plan_below_thirty_percent():
    levels = fib_levels(1.00, 2.00)
    plan = trade_plan(1.55, levels, risk_usd=50)
    assert plan is not None
    assert plan.mode == "pullback"


@pytest.mark.parametrize("low,high", [(2.0, 1.0), (1.0, 1.0)])
def test_no_plan_without_range(low, high):
    assert trade_plan(1.0, fib_levels(low, high)) is None


def test_reentry_guidance_mentions_vwap():
    from penny.fib import reentry_guidance
    text = reentry_guidance(1.20, 1.10, None)
    assert "VWAP" in text
