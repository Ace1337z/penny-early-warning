"""Momentum-build watch feed: rendering, the /builds command, and the throttle."""

from __future__ import annotations

import time

from penny.alerts import format_build_digest, format_build_feed
from penny.scoring import BuildSignal, Metrics, TIER_WATCH


def _signal(symbol="ABCD", **over):
    m = Metrics(symbol=symbol, ts=time.time(), price=1.05, cum_volume=500_000)
    m.prev_close = 1.0
    m.pct_vs_close = 0.05
    m.rise = {15: 0.03, 60: 0.05}
    m.volx = {15: 5.0}
    m.dollar_vol = {15: 20_000}
    m.vwap = 1.0
    m.tier = TIER_WATCH
    m.score = 42
    sig = BuildSignal(metrics=m, score=62.0, stage="ACCELERATING",
                      missing=["volume 5.0x -> 10x"], above_vwap=True)
    for k, v in over.items():
        setattr(sig, k, v)
    return sig


# --- renderers --------------------------------------------------------------

def test_build_feed_leads_with_the_warning_and_the_stage():
    text = format_build_feed([_signal()])
    assert text.startswith("<b>BUILDING NOW")
    assert "not a buy signal" in text
    assert "ABCD" in text
    assert "accelerating" in text.lower()
    assert "to alert:" in text
    assert "not yet alert-tier" in text


def test_build_feed_escapes_dynamic_symbol_text():
    text = format_build_feed([_signal(symbol="A&B")])
    assert "&amp;" in text
    assert "A&B" not in text


def test_build_feed_plain_and_html_carry_the_same_facts():
    plain = format_build_feed([_signal()], html=False)
    html = format_build_feed([_signal()], html=True)
    for token in ("BUILDING NOW", "ABCD", "to alert"):
        assert token in plain and token in html


def test_build_feed_respects_the_limit():
    signals = [_signal(symbol=f"S{i}") for i in range(10)]
    text = format_build_feed(signals, limit=3)
    assert "S0" in text and "S2" in text
    assert "S3" not in text


def test_build_digest_is_empty_without_signals():
    assert format_build_digest([]) == ""


def test_build_digest_names_the_window():
    text = format_build_digest([_signal()], seconds=900)
    assert "MOMENTUM BUILDS" in text
    assert "last 15m" in text
    assert "ABCD" in text


# --- the /builds command ----------------------------------------------------

class _FakeScorer:
    def __init__(self, builds):
        self._builds = builds

    def build_candidates(self, metrics, exclude=None):
        return self._builds


class _FakeEngine:
    def __init__(self, builds):
        self.scorer = _FakeScorer(builds)
        self._last_metrics = [b.metrics for b in builds] or [
            Metrics(symbol="X", ts=0.0, price=1.0)]
        self._builds = builds


class _FakeDB:
    def execute(self, sql, params=()):
        pass

    def query(self, sql, params=()):
        return []

    def kv_get(self, key):
        return None

    def kv_set(self, key, value):
        pass


def _handler_with_telegram(cfg, builds):
    from penny.commands import CommandHandler
    from penny.telegram import FakeTelegram
    telegram = FakeTelegram(cfg)
    handler = CommandHandler(cfg, db=_FakeDB(), journal=None, telegram=telegram,
                             engine=_FakeEngine(builds))
    return handler, telegram


def test_builds_command_lists_candidates_with_progress(cfg):
    handler, telegram = _handler_with_telegram(cfg, [_signal()])
    handler.cmd_builds([])
    text = telegram.outbox[-1]
    assert "MOMENTUM BUILDS" in text
    assert "ABCD" in text
    assert "to alert" in text
    rows = telegram.keyboards[-1]["inline_keyboard"]
    datas = [b["callback_data"] for row in rows for b in row]
    assert "check:ABCD" in datas
    assert "builds" in datas


def test_builds_command_handles_an_empty_tape(cfg):
    handler, telegram = _handler_with_telegram(cfg, [])
    handler.cmd_builds([])
    assert "Nothing building" in telegram.outbox[-1]


def test_builds_callback_edits_in_place(cfg):
    handler, telegram = _handler_with_telegram(cfg, [_signal()])
    cfg.set("TELEGRAM_CHAT_ID", "1")
    handler._handle_callback({"id": "c1", "data": "builds",
                              "message": {"message_id": 9, "chat": {"id": "1"}}})
    assert telegram.edits and telegram.edits[-1]["id"] == 9
    assert "MOMENTUM BUILDS" in telegram.edits[-1]["text"]


# --- the engine feed (throttled, new names only) ----------------------------

def _engine(cfg, tmp_path):
    from penny.engine import Engine
    from penny.telegram import FakeTelegram
    from penny.store import open_journal, open_state
    db = open_state(tmp_path / "state.db")
    journal = open_journal(tmp_path / "journal.db")
    telegram = FakeTelegram(cfg)
    engine = Engine(cfg, db, journal, telegram)
    return engine, telegram, db, journal


def _build_metrics(symbol="ABCD"):
    m = Metrics(symbol=symbol, ts=time.time(), price=1.05, cum_volume=500_000)
    m.prev_close = 1.0
    m.pct_vs_close = 0.05
    m.rise = {15: 0.03, 60: 0.05}
    m.volx = {15: 5.0}
    m.dollar_vol = {15: 20_000}
    m.vwap = 1.0
    m.tier = TIER_WATCH
    m.score = 42
    return m


def test_engine_build_feed_sends_once_then_stays_quiet(cfg, tmp_path):
    cfg.set("BUILD_MIN_SCORE", "0")
    cfg.set("BUILD_MIN_MINUTES", "0")
    engine, telegram, db, journal = _engine(cfg, tmp_path)
    metrics = [_build_metrics()]
    engine._maybe_build_feed("regular", metrics)
    assert any("MOMENTUM BUILDS" in m for m in telegram.outbox)
    before = len(telegram.outbox)
    engine._maybe_build_feed("regular", metrics)  # same names, no new build
    assert len(telegram.outbox) == before
    db.close()
    journal.close()


def test_engine_build_feed_announces_a_reappearing_build(cfg, tmp_path):
    cfg.set("BUILD_MIN_SCORE", "0")
    cfg.set("BUILD_MIN_MINUTES", "0")
    engine, telegram, db, journal = _engine(cfg, tmp_path)
    metrics = [_build_metrics()]
    engine._maybe_build_feed("regular", metrics)
    first = len(telegram.outbox)
    engine._maybe_build_feed("regular", [])      # build fades out
    engine._maybe_build_feed("regular", metrics)  # and comes back
    assert len(telegram.outbox) == first + 1
    db.close()
    journal.close()


def test_engine_build_feed_is_silent_when_disabled(cfg, tmp_path):
    cfg.set("BUILD_FEED", "0")
    cfg.set("BUILD_MIN_SCORE", "0")
    cfg.set("BUILD_MIN_MINUTES", "0")
    engine, telegram, db, journal = _engine(cfg, tmp_path)
    engine._maybe_build_feed("regular", [_build_metrics()])
    assert not any("MOMENTUM BUILDS" in m for m in telegram.outbox)
    db.close()
    journal.close()


def test_engine_build_feed_is_silent_when_closed(cfg, tmp_path):
    cfg.set("BUILD_MIN_SCORE", "0")
    cfg.set("BUILD_MIN_MINUTES", "0")
    engine, telegram, db, journal = _engine(cfg, tmp_path)
    engine._maybe_build_feed("closed", [_build_metrics()])
    assert not any("MOMENTUM BUILDS" in m for m in telegram.outbox)
    db.close()
    journal.close()
