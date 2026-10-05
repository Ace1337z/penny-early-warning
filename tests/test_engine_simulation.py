"""End-to-end: the offline simulation exercises the real engine (acceptance test 1)."""

from __future__ import annotations

import pytest

from penny.simulation import run_simulation


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    home = tmp_path_factory.mktemp("sim")
    return run_simulation(minutes=150, home=home)


def test_simulation_all_checks_pass(report):
    failures = [(name, detail) for name, ok, detail in report.checks if not ok]
    assert not failures, f"failed checks: {failures}"
    assert report.ok is True


def test_alerted_symbols(report):
    symbols = {row["symbol"] for row in report.alerts}
    assert "GRND" in symbols
    assert "BRST" in symbols
    assert not any(s.startswith("NZ") for s in symbols)


def test_cycle_time_is_fast(report):
    if report.cycle_ms:
        assert sum(report.cycle_ms) / len(report.cycle_ms) < 500
