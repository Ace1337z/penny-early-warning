"""Parsers and helpers: CSV/HTML detection, number formats, sessions, holidays, scrubbing."""

from __future__ import annotations

from datetime import datetime

from penny.sources.base import fnum, looks_like_html, parse_csv
from penny.util import ET, mask, scrub, session_for, us_market_holidays


def test_csv_parsing():
    rows = parse_csv("Ticker,Price\nAAA,1.23\nBBB,4.56\n")
    assert rows[0]["Ticker"] == "AAA"
    assert rows[1]["Price"] == "4.56"


def test_html_detection():
    assert looks_like_html("<!DOCTYPE html><html><body>no</body></html>")
    assert not looks_like_html("Ticker,Price\nAAA,1.23")


def test_number_formats():
    assert fnum("$1,234.5") == 1234.5
    assert fnum("12.5M") == 12_500_000
    assert fnum("3.2B") == 3_200_000_000
    assert fnum("-") is None
    assert fnum("") is None
    assert fnum("7.5%") == 7.5


def test_sessions():
    def at(h, m):
        return datetime(2026, 6, 10, h, m, tzinfo=ET)
    assert session_for(at(4, 0)) == "pre"
    assert session_for(at(9, 29)) == "pre"
    assert session_for(at(9, 30)) == "regular"
    assert session_for(at(15, 59)) == "regular"
    assert session_for(at(16, 0)) == "post"
    assert session_for(at(19, 59)) == "post"
    assert session_for(at(20, 0)) == "closed"


def test_holidays_include_observed_dates():
    days = us_market_holidays(2026)
    assert datetime(2026, 1, 1).date() in days
    assert datetime(2026, 12, 25).date() in days
    # 2026-07-04 is a Saturday, observed the preceding Friday.
    assert datetime(2026, 7, 3).date() in days


def test_mask():
    assert mask("abcdef123456") == "abcd******"
    assert mask("") == "(unset)"


def test_scrub():
    text = "https://x/y?auth=SECRET123&z=1 Authorization: Bearer TOPSECRET"
    out = scrub(text)
    assert "SECRET123" not in out
    assert "TOPSECRET" not in out
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    assert token not in scrub(f"token {token} end")


def test_money_and_pct():
    from penny.util import compact, money, pct
    assert money(1.234) == "$1.23"
    assert money(0.5) == "$0.5"
    assert pct(22.7) == "+22.7%"
    assert compact(113_000) == "113K"
    assert compact(2_500_000) == "2.5M"
