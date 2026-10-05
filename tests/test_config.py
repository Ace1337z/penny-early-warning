"""Configuration: defaults, live reload, masked display, permissions."""

from __future__ import annotations

import os
import stat

import pytest

from penny.config import DEFAULTS, SECRET_KEYS, Config


def test_defaults_are_complete():
    assert len(DEFAULTS) > 100
    for key in ("MAX_PRICE", "MIN_PRICE", "ALERT_MIN_TIER", "AI_MODELS", "TELEGRAM_TOKEN"):
        assert key in DEFAULTS


def test_round_trip_and_defaults(tmp_path):
    cfg = Config(tmp_path / "config.env")
    cfg.set("PENNY_HOME", str(tmp_path))
    cfg.set("MAX_PRICE", "7")
    cfg.save()
    again = Config(tmp_path / "config.env")
    assert again.raw("MAX_PRICE") == "7"
    assert again.raw("MIN_PRICE") == DEFAULTS["MIN_PRICE"]


def test_typed_accessors(cfg):
    cfg.set("WORKERS", "4")
    cfg.set("RISK_USD", "12.5")
    cfg.set("ALERT_EXTENDED", "true")
    cfg.set("EXTRA_SYMBOLS", "AAA, BBB ,CCC")
    assert cfg.int("WORKERS") == 4
    assert cfg.float("RISK_USD") == 12.5
    assert cfg.bool("ALERT_EXTENDED") is True
    assert cfg.list("EXTRA_SYMBOLS") == ["AAA", "BBB", "CCC"]


def test_secrets_are_masked(cfg):
    cfg.set("FINVIZ_TOKEN", "abcdef123456")
    shown = cfg.display("FINVIZ_TOKEN")
    assert shown.startswith("abcd")
    assert "123456" not in shown
    assert cfg.display("MAX_PRICE") == "10"


def test_live_reload(tmp_path):
    path = tmp_path / "config.env"
    cfg = Config(path)
    cfg.set("PENNY_HOME", str(tmp_path))
    cfg.save()
    path.write_text(path.read_text() + "\nMAX_PRICE=3\n", encoding="utf-8")
    os.utime(path, (1, 1))  # a positive, different mtime
    assert cfg.reload_if_changed() is True
    assert cfg.raw("MAX_PRICE") == "3"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_config_is_mode_600(tmp_path):
    cfg = Config(tmp_path / "config.env")
    cfg.set("PENNY_HOME", str(tmp_path))
    cfg.save()
    mode = stat.S_IMODE(os.stat(cfg.path).st_mode)
    assert mode == 0o600


def test_derived_paths(tmp_path):
    cfg = Config(tmp_path / "config.env")
    cfg.set("PENNY_HOME", str(tmp_path))
    assert cfg.state_db == tmp_path / "state.db"
    assert cfg.journal_db == tmp_path / "journal.db"
    assert cfg.bars_dir.is_dir()


def test_secrets_lists_only_set_values(cfg):
    cfg.set("FINVIZ_TOKEN", "x")
    assert "x" in cfg.secrets()
