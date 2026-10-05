"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def cfg(tmp_path):
    from penny.config import Config
    c = Config(tmp_path / "config.env")
    c.set("PENNY_HOME", str(tmp_path))
    return c


@pytest.fixture()
def state(tmp_path):
    from penny.store import open_state
    db = open_state(tmp_path / "state.db")
    yield db
    db.close()


@pytest.fixture()
def journal_db(tmp_path):
    from penny.store import open_journal
    db = open_journal(tmp_path / "journal.db")
    yield db
    db.close()
