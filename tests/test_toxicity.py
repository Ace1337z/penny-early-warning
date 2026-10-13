"""SEC toxicity firewall (Spec 1): windows, classes, fail-closed gate integration."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from penny.strategies import evaluate_gate
from penny.strategies.base import Bundle
from penny.strategies.toxicity import (ACTIVE_FINANCING, ATM_ACTIVE,
                                       CLEAN_OR_PASSIVE, RESALE_OVERHANG,
                                       REVERSE_SPLIT_RECENT,
                                       GENERAL_WINDOW_DAYS, classify,
                                       normalize_filing_date, within_windows)

TODAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 10, 15)
STALE = "9/9/2020"                      # the exact leak from the BKYI card


def t(x):
    return classify(x, today=TODAY)


# --- classification ---------------------------------------------------------


def test_reverse_split_vetoes_within_90_days():
    tox = t([{"form": "8-K", "date": "2026-10-01", "items": "5.03",
              "text": "1-for-60 reverse split"}])
    assert tox.veto and tox.risk == REVERSE_SPLIT_RECENT
    assert tox.reason_code == "SEC_VETO_REVERSE_SPLIT"


def test_reverse_split_expires_after_90_days():
    tox = t([{"form": "8-K", "date": "2026-07-01", "items": "5.03",
              "text": "reverse split of 1-for-60"}])
    assert not tox.veto


def test_424b_prospectus_vetoes():
    for form in ("424B3", "424B5"):
        tox = t([{"form": form, "date": "2026-10-02"}])
        assert tox.veto and tox.risk == ACTIVE_FINANCING
        assert tox.reason_code == "SEC_VETO_OFFERING"


def test_8k_item_3_02_vetoes():
    tox = t([{"form": "8-K", "date": "2026-10-05", "items": "3.02,9.01"}])
    assert tox.veto and tox.risk == ACTIVE_FINANCING


def test_8k_item_1_01_needs_financing_keyword():
    assert not t([{"form": "8-K", "date": "2026-10-05", "items": "1.01",
                   "text": "entered a material agreement"}]).veto
    tox = t([{"form": "8-K", "date": "2026-10-05", "items": "1.01",
              "text": "convertible note purchase agreement"}])
    assert tox.veto and tox.risk == ACTIVE_FINANCING


def test_earnings_8k_is_not_financing():
    tox = t([{"form": "8-K", "date": "2026-10-05", "items": "2.02,9.01"}])
    assert tox.risk == CLEAN_OR_PASSIVE and not tox.veto


def test_s1_resale_overhang_vetoes():
    tox = t([{"form": "S-1", "date": "2026-10-02",
              "text": "resale of shares by selling stockholders"}])
    assert tox.veto and tox.risk == RESALE_OVERHANG


def test_s3_naming_an_atm_program_is_elevated_not_vetoed():
    tox = t([{"form": "S-3", "date": "2026-10-02",
              "text": "at-the-market issuance program sales agreement"}])
    assert not tox.veto and tox.risk == ATM_ACTIVE
    assert tox.elevated and tox.reason_code == "SEC_ATM_ACTIVE"


def test_passive_shelf_is_clean():
    tox = t([{"form": "S-3", "date": "2026-10-02"}])
    assert tox.risk == CLEAN_OR_PASSIVE and not tox.veto and not tox.elevated


def test_a_2019_filing_never_leaks_into_today():
    """The BKYI symptom: M/D/YYYY dates compared lexically against ISO let a
    90-day-old (or 6-year-old) filing through the window. After normalisation a
    stale row is simply outside every window."""
    tox = t([{"form": "8-K", "date": STALE, "items": "3.02"},
             {"form": "8-K", "date": "2026-09-08", "items": "5.03",
              "text": "reverse split"}])
    assert tox.veto and tox.risk == REVERSE_SPLIT_RECENT
    only_stale = t([{"form": "8-K", "date": STALE, "items": "3.02"}])
    assert only_stale.risk == CLEAN_OR_PASSIVE


def test_reverse_split_beats_an_offering():
    tox = t([{"form": "424B5", "date": "2026-10-02"},
             {"form": "8-K", "date": "2026-10-01", "items": "5.03",
              "text": "reverse split"}])
    assert tox.risk == REVERSE_SPLIT_RECENT


# --- date helpers -----------------------------------------------------------


def test_normalize_filing_date_handles_both_formats():
    assert normalize_filing_date("2026-10-13").isoformat() == "2026-10-13"
    assert normalize_filing_date("10/13/2026").isoformat() == "2026-10-13"
    assert normalize_filing_date("9/9/2020").isoformat() == "2020-09-09"
    assert normalize_filing_date("n/a") is None


def test_within_windows_keeps_only_recent():
    rows = within_windows([
        {"form": "4", "date": "2026-10-01"},
        {"form": "4", "date": "2026-09-01"},
    ], today=TODAY)
    assert [r["date"] for r in rows] == ["2026-10-01"]


# --- gate integration (fail-closed) ----------------------------------------


def _bundle(**kw):
    d = dict(
        symbol="ABCD", price=1.42, prev_close=1.09, open_price=1.10,
        day_high=1.44, day_low=1.08, cum_volume=9_000_000, pct_vs_close=0.303,
        rise={3: 0.012, 5: 0.035, 15: 0.09, 60: 0.20},
        volx={3: 9.0, 5: 12.4, 15: 6.0},
        dollar_vol={3: 9_000.0, 5: 45_000.0, 15: 210_000.0},
        vwap=1.31, from_high=-0.014, minutes_since_low=38.0,
        minutes_tracked=60.0, session="regular", now_et=NOW,
        float_shares=18_500_000, short_pct=8.4, market_cap=60e6, sector="Biotech",
        dollar_level=1.00,
        news=[{"title": "Acme Corp awarded $12M contract", "source": "Benzinga",
               "url": "https://example.com/pr", "age": "2h"}],
    )
    d.update(kw)
    return Bundle(**d)


def test_gate_vetoes_a_reverse_split_before_the_ai():
    b = _bundle(filings=[{"form": "8-K", "date": "2026-10-01", "items": "5.03",
                          "text": "1-for-60 reverse split"}])
    gate = evaluate_gate(b)
    assert gate.passed is False
    assert gate.blocked_by == "SEC_VETO_REVERSE_SPLIT"


def test_gate_vetoes_an_active_offering():
    b = _bundle(filings=[{"form": "424B5", "date": "2026-10-02"}])
    gate = evaluate_gate(b)
    assert gate.passed is False
    assert gate.blocked_by == "SEC_VETO_OFFERING"


def test_gate_passes_with_no_filings():
    gate = evaluate_gate(_bundle(filings=None))
    assert gate.passed is True


def test_active_atm_flags_the_gate_but_passes():
    b = _bundle(filings=[{"form": "S-3", "date": "2026-10-02",
                          "text": "at-the-market sales agreement"}])
    gate = evaluate_gate(b)
    assert gate.passed is True
    assert "ACTIVE_ATM" in gate.warnings