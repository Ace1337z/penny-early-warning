"""The strategy gate must reject noise and only pass recognisable setups.

Each test is a named trading setup rather than a code path, so a regression
reads as "first green day stopped working" instead of "line 47 changed".
"""

from __future__ import annotations

from datetime import datetime

import pytest

from penny.strategies import (Bundle, classify, evaluate_gate, extract_cause,
                               rank_catalysts, time_of_day_adjustment)
from penny.strategies.gate import no_new_entries
from penny.strategies.news_cause import cause_lines

ET = datetime(2026, 10, 12, 10, 15)          # mid-morning, normal threshold


def bars(pairs, start=1_700_000_000.0, step=60):
    """[(offset_minutes, o, h, l, c, v), ...] -> 1m bars."""
    out = []
    for i, (off, o, h, l, c, v) in enumerate(pairs):
        out.append({"t": start + (off * 60 + i * step), "o": o, "h": h, "l": l,
                    "c": c, "v": v})
    return out


def bundle(**kw):
    d = dict(
        symbol="ABCD", price=1.42, prev_close=1.09, open_price=1.10,
        day_high=1.44, day_low=1.08, cum_volume=9_000_000, pct_vs_close=0.303,
        rise={3: 0.012, 5: 0.035, 15: 0.09, 60: 0.20},
        volx={3: 9.0, 5: 12.4, 15: 6.0},
        dollar_vol={3: 9_000.0, 5: 45_000.0, 15: 210_000.0},
        vwap=1.31, from_high=-0.014, minutes_since_low=38.0,
        minutes_tracked=60.0, session="regular", now_et=ET,
        float_shares=18_500_000, short_pct=8.4, market_cap=60e6, sector="Biotech",
        dollar_level=1.00,
    )
    d.update(kw)
    return Bundle(**d)


def noisy(**kw):
    """The ~60 stocks that must never alert: small move, no volume, no story."""
    return bundle(
        price=1.09, pct_vs_close=0.02, rise={3: 0.001, 5: 0.002, 15: 0.02, 60: 0.02},
        volx={3: 0.9, 5: 1.1, 15: 0.8}, dollar_vol={3: 500.0, 5: 900.0, 15: 3_000.0},
        vwap=1.05, from_high=-0.20, float_shares=90_000_000, short_pct=2.0,
        news=[{"title": "Company provides update on operations",
               "source": "Yahoo", "url": "", "age": "5h"}],
        **kw)


CONTRACT = {"title": "Acme Corp awarded $12M contract", "source": "Benzinga",
            "url": "https://example.com/pr", "age": "2h"}


# --- the gate filters noise --------------------------------------------------

def test_noise_produces_no_alert():
    gate = evaluate_gate(noisy())
    assert gate.passed is False
    assert gate.blocked_by in ("no_strategy", "below_min_score", "no_catalyst")


def test_no_detector_reports_a_short_or_a_fade():
    """Long-only is a property of the whole package, not one branch."""
    gate = evaluate_gate(bundle(news=[CONTRACT]))
    parts = []
    for r in gate.strategies:
        parts.extend(r.reasons)
        parts.extend(r.failed_on)
    text = " ".join(parts).lower()
    for banned in ("short entry", "sell short", "go short", "fade the", "short it"):
        assert banned not in text


def test_a_hard_catalyst_on_volume_passes():
    gate = evaluate_gate(bundle(news=[CONTRACT]))
    assert gate.passed is True
    assert gate.primary == "catalyst_volume"
    assert gate.blocked_by is None


# --- catalyst_volume --------------------------------------------------------

def test_a_catalyst_without_volume_fails():
    """A percentage on no trade is a spread, not a setup."""
    b = bundle(volx={3: 1.0, 5: 1.2, 15: 0.6}, dollar_vol={5: 1_000.0},
               news=[CONTRACT])
    gate = evaluate_gate(b)
    r = gate.by_name("catalyst_volume")
    assert r.verdict == "FAIL"
    assert any("volx" in f for f in r.failed_on)


def test_a_move_with_no_news_now_clears_the_gate_on_a_mechanism():
    """Spec 2.0 A: cause is a label, not a gate.

    The same tape that used to fail on "no catalyst" now passes because the
    mechanism detectors name what ran the move -- here silent accumulation.
    """
    gate = evaluate_gate(bundle(news=[]))
    assert gate.passed is True
    assert gate.primary in ("silent_accumulation", "short_squeeze_mechanics",
                            "sympathy_move")


def test_hard_catalysts_outrank_soft_ones():
    hard = classify("Acme Corp awarded $12M contract")
    soft = classify("Company to present at investor conference")
    assert hard == "HARD"
    assert soft == "SOFT"
    assert classify("FDA approves Acme drug") == "HARD"
    assert classify("Acme announces partnership with named counterparty") == "MEDIUM"


def test_an_offering_is_classified_as_dilution():
    assert classify("Acme prices $12M offering at $1.05 per share") == "DILUTION"
    assert classify("Acme announces public offering") == "DILUTION"
    assert classify("Acme announces reverse split") == "DILUTION"


def test_a_priced_offering_below_the_open_blocks_the_gate():
    b = bundle(news=[{"title": "Acme prices $12M offering at $1.05 per share",
                      "source": "GlobeNewswire", "url": "u", "age": "1h"}])
    gate = evaluate_gate(b)
    assert gate.passed is False
    assert gate.blocked_by == "DILUTION"


def test_an_offering_above_the_open_is_not_a_dilution_block():
    b = bundle(news=[{"title": "Acme prices $12M offering at $1.55 per share",
                      "source": "GlobeNewswire", "url": "u", "age": "1h"}])
    assert evaluate_gate(b).blocked_by != "DILUTION"


# --- low_float_squeeze ------------------------------------------------------

def test_squeeze_passes_with_a_thin_float_and_a_crowded_short():
    b = bundle(float_shares=8_000_000, short_pct=22.0, short_ratio=4.1,
               volx={3: 6.0, 5: 6.0, 15: 5.0}, news=[CONTRACT])
    r = evaluate_gate(b).by_name("low_float_squeeze")
    assert r.verdict == "PASS"


def test_squeeze_now_passes_without_a_catalyst():
    """Spec 2.0 A: the implicit cause check is gone from low_float_squeeze.

    A thin float, a crowded short and real volume are the setup. The news feed
    having nothing is exactly the case the detector exists for, so cause
    absence no longer blocks it.
    """
    b = bundle(float_shares=8_000_000, short_pct=22.0, short_ratio=4.1,
               volx={3: 6.0, 5: 6.0, 15: 5.0}, news=[])
    assert evaluate_gate(b).by_name("low_float_squeeze").verdict == "PASS"


def test_squeeze_fails_when_the_float_is_large():
    b = bundle(float_shares=80_000_000, short_pct=22.0, news=[CONTRACT])
    r = evaluate_gate(b).by_name("low_float_squeeze")
    assert r.verdict == "FAIL"
    assert "float" in r.failed_on[0]


def test_squeeze_reports_insufficient_data_without_short_data():
    """Not knowing the short interest is not the same as it being low."""
    b = bundle(float_shares=8_000_000, short_pct=None, short_ratio=None,
               news=[CONTRACT])
    assert evaluate_gate(b).by_name("low_float_squeeze").verdict == "INSUFFICIENT_DATA"


# --- dollar_breakout --------------------------------------------------------

def test_dollar_breakout_passes_when_reclaiming_one_dollar():
    b = bundle(price=1.02, prev_close=0.94, open_price=0.93, pct_vs_close=0.085,
               volx={3: 5.0, 5: 5.5, 15: 4.5}, news=[CONTRACT])
    assert evaluate_gate(b).by_name("dollar_breakout").verdict == "PASS"


def test_dollar_breakout_fails_when_the_stock_was_already_above_one_dollar():
    b = bundle(price=3.10, prev_close=3.00, open_price=3.00, news=[CONTRACT])
    r = evaluate_gate(b).by_name("dollar_breakout")
    assert r.verdict == "FAIL"
    assert "not <" in r.failed_on[0]


# --- float_rotation ---------------------------------------------------------

def test_float_rotation_passes_at_double_the_float():
    b = bundle(float_shares=5_000_000, cum_volume=10_000_000)
    assert evaluate_gate(b).by_name("float_rotation").verdict == "PASS"


def test_float_rotation_fails_at_1_2_times_the_float():
    b = bundle(float_shares=5_000_000, cum_volume=6_000_000)
    assert evaluate_gate(b).by_name("float_rotation").verdict == "FAIL"


# --- vwap_reclaim -----------------------------------------------------------

def test_vwap_reclaim_passes_after_ten_minutes_below_vwap():
    # Twelve 1m bars under 1.40, then the reclaim on heavy volume.
    seq = bars([(i, 1.39, 1.39, 1.38, 1.39, 500) for i in range(12)]
               + [(12, 1.39, 1.43, 1.39, 1.42, 4000)])
    b = bundle(bars_1m=seq, price=1.42, vwap=1.40,
               volx={3: 2.3, 5: 2.4, 15: 1.9})
    assert evaluate_gate(b).by_name("vwap_reclaim").verdict == "PASS"


def test_vwap_reclaim_fails_on_thin_reclaim_volume():
    seq = bars([(i, 1.39, 1.39, 1.38, 1.39, 500) for i in range(12)]
               + [(12, 1.39, 1.43, 1.39, 1.42, 600)])
    b = bundle(bars_1m=seq, price=1.42, vwap=1.40,
               volx={3: 1.1, 5: 1.2, 15: 0.9})
    assert evaluate_gate(b).by_name("vwap_reclaim").verdict == "FAIL"


def test_vwap_reclaim_reports_insufficient_data_without_bars():
    """Substituting time-since-low here would pass every stock off its low."""
    b = bundle(price=1.42, vwap=1.40, minutes_since_low=12.0,
               volx={3: 2.3, 5: 2.4, 15: 1.9})
    assert evaluate_gate(b).by_name("vwap_reclaim").verdict == "INSUFFICIENT_DATA"


# --- first_green_day --------------------------------------------------------

HISTORY = [
    {"day": "2026-10-05", "open": 1.05, "high": 1.06, "low": 0.98, "close": 1.00, "volume": 1e6},
    {"day": "2026-10-06", "open": 1.00, "high": 1.01, "low": 0.97, "close": 0.99, "volume": 1e6},
    {"day": "2026-10-07", "open": 0.99, "high": 1.00, "low": 0.96, "close": 0.98, "volume": 1e6},
    {"day": "2026-10-08", "open": 0.98, "high": 0.99, "low": 0.95, "close": 0.97, "volume": 1e6},
    {"day": "2026-10-09", "open": 0.97, "high": 0.98, "low": 0.96, "close": 0.98, "volume": 1e6},
    {"day": "2026-10-09", "open": 0.98, "high": 1.00, "low": 0.97, "close": 0.99, "volume": 1e6},
]


def test_first_green_day_passes_after_five_flat_sessions():
    b = bundle(pct_vs_close=0.31, daily_history=HISTORY, news=[CONTRACT])
    assert evaluate_gate(b).by_name("first_green_day").verdict == "PASS"


def test_first_green_day_fails_on_a_second_green_day():
    """The whole point of the setup: the first big green day, not the second."""
    rising = [dict(h) for h in HISTORY]
    for h in rising[-3:]:
        h["close"] = h["close"] * 1.20
    b = bundle(pct_vs_close=0.31, daily_history=rising, news=[CONTRACT])
    assert evaluate_gate(b).by_name("first_green_day").verdict == "FAIL"


def test_first_green_day_reports_insufficient_data_without_history():
    b = bundle(news=[CONTRACT])
    assert evaluate_gate(b).by_name("first_green_day").verdict == "INSUFFICIENT_DATA"


def test_insufficient_data_is_never_treated_as_a_pass():
    """A detector that could not read its input must not pass the gate."""
    b = bundle(news=[CONTRACT], daily_history=[])
    gate = evaluate_gate(b)
    r = gate.by_name("first_green_day")
    assert r.verdict == "INSUFFICIENT_DATA"
    assert r.verdict != "PASS"


# --- failed_breakout --------------------------------------------------------

def test_failed_breakout_passes_on_a_clean_break_fail_reclaim():
    level = 1.40
    seq = bars([
        (0, 1.30, 1.39, 1.29, 1.38, 1000),
        (5, 1.38, 1.41, 1.37, 1.40, 2000),      # broke 1.40 on a close
        (10, 1.40, 1.40, 1.37, 1.38, 1500),     # failed back below
        (15, 1.38, 1.39, 1.36, 1.37, 1200),     # washout
        (20, 1.37, 1.42, 1.37, 1.41, 5000),     # reclaimed on 2.5x
        (25, 1.41, 1.43, 1.40, 1.42, 3000),
        (30, 1.42, 1.44, 1.41, 1.43, 3000),
    ])
    # The level the stock broke is 1.40, so that is what VWAP must be: the
    # detector reads levels off the quote, it does not invent them.
    b = bundle(bars_1m=seq, day_high=1.44, vwap=1.40, volx={3: 4.0, 5: 4.0, 15: 3.0})
    assert evaluate_gate(b).by_name("failed_breakout").verdict == "PASS"


def test_failed_breakout_fails_without_a_reclaim():
    # Broke 1.40 and kept going. Six bars, because fewer is not enough history
    # to judge the pattern at all (that is INSUFFICIENT_DATA, a different answer).
    seq = bars([
        (0, 1.30, 1.39, 1.29, 1.38, 1000),
        (5, 1.38, 1.41, 1.37, 1.40, 2000),
        (10, 1.40, 1.40, 1.35, 1.36, 5000),     # broke down and kept going
        (15, 1.36, 1.37, 1.33, 1.34, 4000),
        (20, 1.34, 1.35, 1.31, 1.32, 3000),
        (25, 1.32, 1.33, 1.30, 1.31, 2500),
    ])
    b = bundle(bars_1m=seq, day_high=1.44, vwap=1.40, volx={3: 4.0, 5: 4.0, 15: 3.0})
    assert evaluate_gate(b).by_name("failed_breakout").verdict == "FAIL"


# --- red_to_green and WORG --------------------------------------------------

def test_red_to_green_passes_on_an_intraday_reversal():
    b = bundle(open_price=1.05, prev_close=1.09, day_low=1.03, price=1.12,
               vwap=1.08, volx={3: 3.0, 5: 3.2, 15: 2.8}, minutes_since_low=20.0)
    assert evaluate_gate(b).by_name("red_to_green").verdict == "PASS"


def test_red_to_green_fails_when_it_opened_green():
    b = bundle(open_price=1.12, prev_close=1.09)
    assert evaluate_gate(b).by_name("red_to_green").verdict == "FAIL"


def test_worg_passes_on_a_strong_prior_close_after_a_multiweek_breakout():
    hist = [
        {"day": f"2026-10-0{i+1}", "open": 1.0, "high": 1.0, "low": 0.95,
         "close": 0.97 + i * 0.01, "volume": 1e6} for i in range(5)
    ]
    hist.append({"day": "2026-10-06", "open": 0.98, "high": 1.10, "low": 0.97,
                 "close": 1.09, "volume": 2e6})
    b = bundle(open_price=1.05, prev_close=1.09, day_low=1.03, price=1.12,
               vwap=1.08, volx={3: 3.0, 5: 3.2, 15: 2.8}, minutes_since_low=20.0,
               daily_history=hist)
    assert evaluate_gate(b).by_name("worg").verdict == "PASS"


def test_worg_fails_without_a_multiweek_breakout():
    hist = [dict(h, close=0.90) for h in HISTORY]
    b = bundle(daily_history=hist, news=[CONTRACT])
    assert evaluate_gate(b).by_name("worg").verdict == "FAIL"


# --- day3_surge -------------------------------------------------------------

def test_day3_surge_passes_when_day2_held_on_volume():
    hist = [
        {"day": "2026-10-08", "open": 1.00, "high": 1.28, "low": 0.99,
         "close": 1.25, "volume": 10_000_000},
        {"day": "2026-10-09", "open": 1.24, "high": 1.30, "low": 1.00,
         "close": 1.22, "volume": 5_500_000},
    ]
    b = bundle(daily_history=hist, day_high=1.30, price=1.28, news=[CONTRACT])
    assert evaluate_gate(b).by_name("day3_surge").verdict == "PASS"


def test_day3_surge_fails_when_day2_volume_was_dead():
    hist = [
        {"day": "2026-10-08", "open": 1.00, "high": 1.28, "low": 0.99,
         "close": 1.25, "volume": 10_000_000},
        {"day": "2026-10-09", "open": 1.24, "high": 1.26, "low": 1.00,
         "close": 1.22, "volume": 1_000_000},
    ]
    b = bundle(daily_history=hist, day_high=1.30, price=1.28, news=[CONTRACT])
    assert evaluate_gate(b).by_name("day3_surge").verdict == "FAIL"


# --- triggers never pass alone ----------------------------------------------

def test_a_trigger_alone_does_not_pass_the_gate():
    b = noisy()
    b.volx = {3: 3.0, 5: 3.0, 15: 3.0}
    b.rise = {3: 0.03, 5: 0.03, 15: 0.03, 60: 0.03}
    gate = evaluate_gate(b)
    if gate.triggers():
        assert gate.passed is False
        assert gate.blocked_by in ("no_strategy", "below_min_score", "no_catalyst")


def test_a_trigger_is_only_named_when_a_primary_passed():
    b = bundle(news=[CONTRACT])
    gate = evaluate_gate(b)
    if gate.entry_trigger:
        assert gate.passed is True


# --- exhaustion and dilution blockers ---------------------------------------

def test_exhaustion_blocks_a_new_long():
    b = bundle(price=1.43, day_high=1.44, from_high=-0.007, vwap=1.20,
               volx={3: 1.2, 5: 1.4, 15: 1.1}, news=[CONTRACT])
    gate = evaluate_gate(b)
    assert gate.passed is False
    assert gate.blocked_by == "EXHAUSTED"


def test_exhaustion_does_not_block_while_volume_still_confirms():
    b = bundle(price=1.43, day_high=1.44, from_high=-0.007, vwap=1.20,
               volx={3: 9.0, 5: 12.4, 15: 6.0}, news=[CONTRACT])
    assert evaluate_gate(b).blocked_by != "EXHAUSTED"


def test_a_data_conflict_blocks_everything():
    b = bundle(data_conflict=True, news=[CONTRACT])
    gate = evaluate_gate(b)
    assert gate.passed is False
    assert gate.blocked_by == "DATA_CONFLICT"


# --- time-of-day ------------------------------------------------------------

def test_the_open_window_requires_a_higher_score():
    assert time_of_day_adjustment(datetime(2026, 10, 12, 9, 45)) == 10.0


def test_lunch_requires_a_higher_score():
    assert time_of_day_adjustment(datetime(2026, 10, 12, 12, 30)) == 5.0


def test_the_close_window_requires_a_higher_score():
    assert time_of_day_adjustment(datetime(2026, 10, 12, 15, 40)) == 10.0


def test_mid_morning_and_power_hour_use_the_normal_threshold():
    assert time_of_day_adjustment(datetime(2026, 10, 12, 10, 45)) == 0.0
    assert time_of_day_adjustment(datetime(2026, 10, 12, 14, 30)) == 0.0


def test_the_time_filter_can_be_switched_off():
    assert time_of_day_adjustment(datetime(2026, 10, 12, 9, 45), False) == 0.0


def test_no_new_entries_after_1550():
    assert no_new_entries(datetime(2026, 10, 12, 15, 55)) is True
    assert no_new_entries(datetime(2026, 10, 12, 15, 40)) is False


MARGINAL = {"title": "Acme Corp awarded $12M contract", "source": "Benzinga",
            "url": "u", "age": "9h"}


def marginal_bundle(now):
    """A setup whose score sits inside the band the three session windows
    disagree about -- the same facts, a different hour, a different answer.
    The subject here is the time-of-day filter, so the base min_score is set one
    point below the actual score and the window adjustment is what moves it."""
    fresh = {"title": "Acme Corp acquires Beta Industries", "source": "Benzinga",
             "url": "u", "age": "45m"}
    return bundle(volx={3: 3.1, 5: 3.2, 15: 3.1}, news=[fresh], now_et=now)


MARGINAL_MIN_SCORE = 74.0            # just under the bundle's ~74.4 score


def test_the_same_setup_is_judged_differently_by_the_hour():
    mid = evaluate_gate(marginal_bundle(datetime(2026, 10, 12, 10, 45)),
                        min_score=MARGINAL_MIN_SCORE)
    opening = evaluate_gate(marginal_bundle(datetime(2026, 10, 12, 9, 45)),
                            min_score=MARGINAL_MIN_SCORE)
    assert mid.passed is True
    assert mid.gate_score < MARGINAL_MIN_SCORE + 10
    assert opening.passed is False
    assert opening.blocked_by == "below_min_score"
    assert opening.gate_score == mid.gate_score      # same evidence, stricter bar


def test_the_close_window_blocks_what_the_morning_allowed():
    closing = evaluate_gate(marginal_bundle(datetime(2026, 10, 12, 15, 40)),
                            min_score=MARGINAL_MIN_SCORE)
    assert closing.passed is False
    assert closing.blocked_by == "below_min_score"


# --- one cause, not a dump --------------------------------------------------

def test_exactly_one_cause_line_is_ever_emitted():
    b = bundle(news=[CONTRACT, CONTRACT, CONTRACT])
    _, ranked, _ = rank_catalysts(b)
    lines = cause_lines(extract_cause(ranked, b))
    assert sum(1 for line in lines if line.startswith("CAUSE:")) == 1


def test_a_move_with_no_aligned_catalyst_says_none_found():
    b = bundle()
    cause = extract_cause([], b)
    assert cause.kind == "UNEXPLAINED"
    assert "none found" in cause.text


def test_a_catalyst_names_its_source_and_url():
    b = bundle(news=[CONTRACT])
    _, ranked, _ = rank_catalysts(b)
    lines = cause_lines(extract_cause(ranked, b))
    text = " ".join(lines)
    assert "Benzinga" in text
    assert "https://example.com/pr" in text
    assert "CONFIDENCE" in text


def test_a_hard_catalyst_is_named_inside_the_window_even_after_decay():
    # SKYA regression: a tender offer at 3h decays to ~0.50, below the 0.6
    # floor, and the flat floor silently returned "unidentified" while the
    # headline sat right there. HARD must stay nameable for the whole 4h window.
    offer = {"title": "SkyAI Plans $201 Million Cash Tender Offer at $3 Per Share",
             "source": "Finviz", "url": "https://example.com/offer", "age": "3h"}
    b = bundle(news=[offer])
    _, ranked, _ = rank_catalysts(b)
    assert ranked[0]["weight"] < 0.6            # decayed below the old floor
    cause = extract_cause(ranked, b)
    assert cause.kind == "CATALYST"
    assert "Tender Offer" in cause.text


def test_social_only_items_do_not_become_a_cause():
    b = bundle(news=[{"title": "trending on social media says to buy",
                      "source": "Yahoo", "url": "", "age": "1h"}])
    _, ranked, _ = rank_catalysts(b)
    assert extract_cause(ranked, b).kind == "UNEXPLAINED"


# --- robustness -------------------------------------------------------------

def test_a_detector_that_raises_does_not_silence_the_others():
    import penny.strategies.gate as gate_mod

    def boom(b):
        raise RuntimeError("malformed bar")

    original = gate_mod.PRIMARY["bull_flag"]
    gate_mod.PRIMARY["bull_flag"] = boom
    try:
        gate = evaluate_gate(bundle(news=[CONTRACT]))
        assert gate.passed is True
        assert gate.by_name("bull_flag").verdict == "INSUFFICIENT_DATA"
    finally:
        gate_mod.PRIMARY["bull_flag"] = original


def test_scores_are_always_within_range():
    for b in (bundle(news=[CONTRACT]), noisy()):
        for r in evaluate_gate(b).strategies:
            assert 0.0 <= r.score <= 100.0
