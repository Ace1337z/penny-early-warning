"""Journal: events, batch import, bar download, metrics and rule search."""

from __future__ import annotations

from datetime import datetime, timedelta

from penny.journal import PRELIMINARY_WINNERS, Journal
from penny.util import ET


def _bars_for_day(day, event_day=None, prev_close=1.00):
    """1-minute bars. On the event day: pre-market, a burst to +50% at 10:00, then a fade.
    Other days are flat at the reference close so the reference close is unambiguous."""
    start = datetime(day.year, day.month, day.day, 4, 0, tzinfo=ET)
    is_event = event_day is None or day == event_day
    bars = []
    for i in range(16 * 60):
        ts = start + timedelta(minutes=i)
        if not is_event:
            price, vol = prev_close, 300
        elif i < 330:                      # 04:00 - 09:29 pre-market
            price = prev_close + 0.05 * (i / 330)
            vol = 500
        elif i < 360:                      # 09:30 - 09:59 the burst
            price = 1.05 + (i - 330) * (0.45 / 30)
            vol = 6000
        elif i < 720:                      # 10:00 - 15:59 the fade
            price = max(1.10, 1.50 - (i - 360) * (0.40 / 360))
            vol = 2000
        else:                              # 16:00 - 19:59 post
            price, vol = 1.10, 200
        prev = bars[-1]["close"] if bars else price
        bars.append({"time": int(ts.timestamp()), "open": prev,
                     "high": round(max(prev, price) * 1.002, 5),
                     "low": round(min(prev, price) * 0.998, 5),
                     "close": round(price, 5), "volume": vol})
    return bars


EVENT_DAY = datetime(2026, 3, 4).date()


class FakeJournal(Journal):
    def _fetch_day(self, symbol, d):
        return _bars_for_day(d, event_day=EVENT_DAY)


def _journal(cfg, journal_db):
    j = FakeJournal(cfg, journal_db)
    return j


def test_add_and_update_event(cfg, journal_db):
    j = _journal(cfg, journal_db)
    j.add_event("grnd", "2026-03-04", "w", pattern="burst", peak_pct=180)
    row = journal_db.query_one("SELECT * FROM events WHERE symbol='GRND'")
    assert row["label"] == "winner"
    assert row["pattern"] == "burst"
    assert row["peak_pct"] == 180
    # A user event is not overwritten by a second user event without overwrite.
    j.add_event("GRND", "2026-03-04", "loser")
    assert journal_db.query_one("SELECT label FROM events WHERE symbol='GRND'")["label"] == "winner"


def test_batch_import(cfg, journal_db):
    j = _journal(cfg, journal_db)
    text = "# comment\nAAA 2026-03-04 w burst 120\nBBB 2026-03-04 l\nbadline\n"
    assert j.import_batch(text) == 2
    labels = j.labels()
    assert labels["winners"] == 1 and labels["losers"] == 1
    assert labels["trust"] == "PRELIMINARY"


def test_trust_thresholds(cfg, journal_db):
    j = _journal(cfg, journal_db)
    for i in range(PRELIMINARY_WINNERS):
        j.add_event(f"W{i:03}", "2026-03-04", "w")
    assert j.labels()["trust"] == "PRELIMINARY"
    for i in range(100):
        j.add_event(f"L{i:03}", "2026-03-04", "l")
    assert j.labels()["trust"] == "MODERATE"


def test_download_and_metrics(cfg, journal_db):
    j = _journal(cfg, journal_db)
    j.add_event("GRND", "2026-03-04", "w")
    bars = j.download_bars("GRND", "2026-03-04")
    assert bars
    m = j.metrics_for("GRND", "2026-03-04")
    assert m is not None
    assert abs(m.peak_pct - 50.0) < 2.0
    assert m.reference_close == 1.00
    assert m.pattern in ("burst", "grinder", "other")
    assert m.preopen_pct is not None and m.preopen_pct > 0
    assert m.drawdown_after_peak is not None and m.drawdown_after_peak < 0


def test_annotate_metrics_writes_json(cfg, journal_db):
    import json
    j = _journal(cfg, journal_db)
    j.add_event("GRND", "2026-03-04", "w")
    j.download_bars("GRND", "2026-03-04")
    j.annotate_metrics("GRND", "2026-03-04")
    row = journal_db.query_one("SELECT metrics FROM events WHERE symbol='GRND'")
    assert json.loads(row["metrics"])["peak_pct"] is not None


def test_rule_search_returns_results(cfg, journal_db):
    j = _journal(cfg, journal_db)
    j.add_event("GRND", "2026-03-04", "w")
    j.add_event("QUIET", "2026-03-04", "l")
    j.download_bars("GRND", "2026-03-04")
    results = j.rule_search(limit=10)
    assert isinstance(results, list)
    if results:
        assert "rise>=" in results[0].describe()
