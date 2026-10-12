"""Phase 2 Telegram commands: /model and /postmortem.

`/model` is the bot-side replacement for the CLI's `penny models`: the reader
only ever needs two things from the model list -- which id is running, and how
to point the alert at a different one -- so both live behind one command and
the switch takes effect on the next alert, without a restart.

`/postmortem SYM [DATE]` prints the full paragraph for one losing alert: the
one-line hint in the EOD is a pointer, this is the finding.
"""

from __future__ import annotations

import pytest

from penny.commands import CommandHandler
from penny.store import open_state
from penny.telegram import FakeTelegram
from penny.tracking import Tracker
from penny.util import to_et


TS = 1_760_000_000.0
DAY = "2026-10-12"


class _DB:
    """The commands only touch watch/kv here; everything else is unused."""

    def execute(self, sql, params=()):
        pass

    def query(self, sql, params=()):
        return []

    def kv_get(self, key):
        return None

    def kv_set(self, key, value):
        pass


class _Engine:
    """Bare engine: no gateway, no tracker until a test attaches one."""

    def status_text(self):
        return "STATUS\nuptime 1h | session regular"


def _handler(tmp_path, cfg):
    telegram = FakeTelegram(cfg)
    handler = CommandHandler(cfg, db=_DB(), journal=None, telegram=telegram,
                             engine=_Engine())
    cfg.set("TELEGRAM_CHAT_ID", "1")
    return handler, telegram


# --- /model ------------------------------------------------------------------

def test_model_reports_which_decision_model_is_running(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    cfg.set("AI_MODEL", "old-model")
    handler.cmd_model([])
    text = telegram.messages[-1]["text"]
    assert "MODEL" in text
    assert "old-model" in text
    # with no provider list available the reader is told how to set one anyway
    assert "/model MODEL-ID" in text


def test_model_switches_the_model_without_a_restart(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    cfg.set("AI_MODEL", "old-model")
    handler.cmd_model(["new-model"])

    assert cfg.raw("AI_MODEL") == "new-model"
    # persisted, not just held in memory
    saved = (tmp_path / "config.env").read_text(encoding="utf-8")
    assert "new-model" in saved

    handler.cmd_model([])
    text = telegram.messages[-1]["text"]
    assert "new-model" in text
    assert "old-model" not in text


def test_model_rejects_an_id_containing_spaces(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    handler.cmd_model(["two words"])
    assert "Usage:" in telegram.messages[-1]["text"]


# --- /postmortem -------------------------------------------------------------

def _tracker_with_an_aborted_alert(tmp_path, handler):
    db = open_state(tmp_path / "state.db")
    tracker = Tracker(db, clock=lambda: TS)
    handler.engine.tracker = tracker
    tracker.start("ABCD", alert_ts=TS, alert_price=1.42, day=DAY,
                  primary="low_float_squeeze")
    tracker.abort("ABCD", reason="trading halted", ts=TS, day=DAY)
    return db, tracker


def test_postmortem_prints_the_full_paragraph(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    db, _ = _tracker_with_an_aborted_alert(tmp_path, handler)
    handler.cmd_postmortem(["ABCD", DAY])
    text = telegram.messages[-1]["text"]
    assert "ABCD entered on low_float_squeeze" in text
    assert "R3 halt clause should have blocked" in text
    assert "Suggest:" in text
    db.close()


def test_postmortem_without_a_date_defaults_to_today(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    db = open_state(tmp_path / "state.db")
    tracker = Tracker(db, clock=lambda: TS)
    handler.engine.tracker = tracker
    today = to_et().strftime("%Y-%m-%d")
    tracker.start("ABCD", alert_ts=TS, alert_price=1.42, day=today,
                  primary="low_float_squeeze")
    tracker.abort("ABCD", reason="trading halted", ts=TS, day=today)

    handler.cmd_postmortem(["ABCD"])
    assert "low_float_squeeze" in telegram.messages[-1]["text"]
    db.close()


def test_postmortem_says_so_for_a_winner_or_an_unknown_symbol(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    db = open_state(tmp_path / "state.db")
    tracker = Tracker(db, clock=lambda: TS)
    handler.engine.tracker = tracker

    handler.cmd_postmortem(["ZZZZ", DAY])
    assert "No tracked alert" in telegram.messages[-1]["text"]

    tracker.start("WINN", alert_ts=TS, alert_price=0.50, day=DAY,
                  primary="gap_and_go")
    tracker.update("WINN", price=0.55, high=0.56, low=0.49, day=DAY)
    handler.cmd_postmortem(["WINN", DAY])
    assert "No losses for" in telegram.messages[-1]["text"]
    db.close()


def test_postmortem_requires_a_symbol(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    handler.cmd_postmortem([])
    assert "Usage:" in telegram.messages[-1]["text"]
