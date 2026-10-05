"""Backup: create, verify, encrypt, restore and the drill."""

from __future__ import annotations

import time

from penny.backup import BackupManager
from penny.config import Config
from penny.store import open_journal, open_state


def _manager(tmp_path, passphrase="correct horse battery staple"):
    cfg = Config(tmp_path / "backup.env")
    cfg.set("PENNY_HOME", str(tmp_path))
    cfg.set("BACKUP_LOCAL_DIR", str(tmp_path / "backups"))
    cfg.set("BACKUP_PASSPHRASE", passphrase)
    cfg.save()
    state = open_state(cfg.state_db)
    journal = open_journal(cfg.journal_db)
    state.execute("INSERT INTO alerts(ts, symbol, tier, price, pct, score) "
                  "VALUES(?,?,?,?,?,?)", (time.time(), "TEST", 2, 1.23, 23.0, 55.0))
    journal.execute("INSERT INTO events(symbol, date, label) VALUES('TEST','2026-01-02','winner')")
    return cfg, state, journal, BackupManager(cfg, state, journal)


def test_backup_created_and_verified(tmp_path):
    _, state, journal, manager = _manager(tmp_path)
    try:
        result = manager.run("test")
        assert result.status == "ok"
        assert result.verified is True
        assert result.size > 0
        assert result.path.endswith((".gpg", ".penny.enc"))
    finally:
        state.close()
        journal.close()


def test_archive_is_encrypted_and_has_no_plaintext(tmp_path):
    _, state, journal, manager = _manager(tmp_path)
    try:
        result = manager.run("test")
        raw = open(result.path, "rb").read()
        assert b"correct horse battery staple" not in raw
        assert not raw.startswith(b"\x1f\x8b")  # not a plain gzip
    finally:
        state.close()
        journal.close()


def test_restore_round_trip(tmp_path):
    _, state, journal, manager = _manager(tmp_path)
    try:
        result = manager.run("test")
        restored = manager.restore(result.path, scope="learning")
        assert restored["ok"] is True
        assert restored["summary"]["events"] == 1
        assert restored["summary"]["alerts"] >= 1
    finally:
        state.close()
        journal.close()


def test_wrong_passphrase_fails(tmp_path):
    _, state, journal, manager = _manager(tmp_path)
    try:
        result = manager.run("test")
        wrong = Config(tmp_path / "wrong.env")
        wrong.set("PENNY_HOME", str(tmp_path))
        wrong.set("BACKUP_PASSPHRASE", "the wrong passphrase")
        bad = BackupManager(wrong, state, journal)
        assert bad.restore(result.path, scope="learning")["ok"] is False
    finally:
        state.close()
        journal.close()


def test_drill_passes(tmp_path):
    _, state, journal, manager = _manager(tmp_path)
    try:
        manager.run("test")
        assert manager.drill()["ok"] is True
    finally:
        state.close()
        journal.close()


def test_overdue_alert_fires_when_no_backup(tmp_path):
    cfg = Config(tmp_path / "b.env")
    cfg.set("PENNY_HOME", str(tmp_path))
    cfg.set("BACKUP_LOCAL_DIR", str(tmp_path / "backups"))
    state = open_state(cfg.state_db)
    journal = open_journal(cfg.journal_db)

    class FakeTelegram:
        def __init__(self):
            self.outbox = []

        def send(self, text, chat_id=None, disable_preview=True):
            self.outbox.append(text)
            return len(self.outbox)

    telegram = FakeTelegram()
    manager = BackupManager(cfg, state, journal, telegram)
    try:
        assert manager.last_verified_age_hours() is None
        manager._alert_overdue()
        assert any("BACKUP OVERDUE" in m for m in telegram.outbox)
    finally:
        state.close()
        journal.close()
