"""SQLite storage: schema, migrations, and small typed helpers for both databases."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    tier INTEGER NOT NULL,
    price REAL,
    pct REAL,
    score REAL,
    msg_id INTEGER,
    alert2_id INTEGER,
    shariah TEXT,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_symbol_ts ON alerts(symbol, ts);

CREATE TABLE IF NOT EXISTS watch (
    symbol TEXT PRIMARY KEY,
    entry REAL,
    stop REAL,
    target REAL,
    ts REAL
);

CREATE TABLE IF NOT EXISTS tokens (
    day TEXT NOT NULL,
    model TEXT NOT NULL,
    kind TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    weighted INTEGER NOT NULL DEFAULT 0,
    invalid INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model, kind)
);

CREATE TABLE IF NOT EXISTS cycles (
    ts REAL PRIMARY KEY,
    ms REAL,
    quotes INTEGER,
    rss_mb REAL,
    api_calls INTEGER,
    alerts INTEGER
);

CREATE TABLE IF NOT EXISTS daily (
    day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    max_tier INTEGER,
    peak_pct REAL,
    first_ts REAL,
    first_price REAL,
    first_tier INTEGER,
    PRIMARY KEY (day, symbol)
);

CREATE TABLE IF NOT EXISTS ai_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    kind TEXT NOT NULL,
    ok INTEGER,
    latency_ms REAL,
    tokens INTEGER,
    error TEXT,
    cached INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ai_calls_model ON ai_calls(model);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER,
    symbol TEXT NOT NULL,
    model TEXT NOT NULL,
    ts REAL NOT NULL,
    session TEXT,
    price0 REAL,
    horizon TEXT NOT NULL,
    due_ts REAL,
    pred_price REAL,
    pred_dir TEXT,
    exp_peak REAL,
    exp_low REAL,
    active INTEGER DEFAULT 0,
    actual_price REAL,
    actual_dir TEXT,
    actual_high REAL,
    actual_low REAL,
    hit INTEGER,
    abs_err REAL,
    peak_err REAL,
    low_err REAL,
    status TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_pred_due ON predictions(status, due_ts);
CREATE INDEX IF NOT EXISTS idx_pred_model ON predictions(model);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS leaderboard_history (
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    stage INTEGER,
    evaluated INTEGER,
    hit_rate REAL,
    mape REAL,
    composite REAL,
    valid_json_rate REAL,
    latency_ms REAL,
    multiplier REAL,
    baseline REAL
);

CREATE TABLE IF NOT EXISTS compliance (
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT,
    as_of TEXT,
    fetched_at REAL,
    detail TEXT,
    PRIMARY KEY (symbol, source)
);

CREATE TABLE IF NOT EXISTS backups (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    destination TEXT,
    path TEXT,
    size INTEGER,
    sha256 TEXT,
    status TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS suppressed (
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    tier INTEGER,
    reason TEXT,
    price REAL,
    pct REAL
);
"""

JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    label TEXT,
    pattern TEXT,
    peak_pct REAL,
    entry REAL,
    exit REAL,
    notes TEXT,
    source TEXT,
    fetched INTEGER DEFAULT 0,
    shariah TEXT,
    metrics TEXT,
    UNIQUE(symbol, date)
);
"""


class Database:
    """Thread-safe-ish SQLite wrapper (one connection per thread, WAL mode)."""

    def __init__(self, path: str | Path, schema: str = "", name: str = "db"):
        self.path = Path(path)
        self.name = name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._schema = schema
        self._init_lock = threading.Lock()
        self._initialized = False
        self._init()

    # -- connections --------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _init(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            if self._schema:
                conn = self._conn()
                conn.executescript(self._schema)
                cur = conn.execute("SELECT COUNT(*) AS c FROM schema_version")
                if cur.fetchone()["c"] == 0:
                    conn.execute("INSERT INTO schema_version(version) VALUES (?)",
                                 (SCHEMA_VERSION,))
                conn.commit()
            self._initialized = True

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- primitives ---------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        conn = self._conn()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        conn = self._conn()
        conn.executemany(sql, [tuple(r) for r in rows])
        conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn().execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        row = self._conn().execute(sql, tuple(params)).fetchone()
        return row

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        val = row[0]
        return default if val is None else val

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # -- kv helpers ---------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM kv WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def integrity_check(self) -> str:
        return str(self.scalar("PRAGMA integrity_check", default="unknown"))

    def row_counts(self) -> dict[str, int]:
        tables = [r["name"] for r in self.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        out = {}
        for t in tables:
            try:
                out[t] = int(self.scalar(f"SELECT COUNT(*) FROM {t}", default=0))
            except sqlite3.Error:
                pass
        return out

    def schema_version(self) -> int:
        try:
            return int(self.scalar("SELECT MAX(version) FROM schema_version", default=0))
        except sqlite3.Error:
            return 0


def open_state(path: str | Path) -> Database:
    return Database(path, STATE_SCHEMA, name="state")


def open_journal(path: str | Path) -> Database:
    return Database(path, JOURNAL_SCHEMA, name="journal")


def sqlite_backup(src: Database, dest_path: str | Path) -> None:
    """Consistent online backup of a live SQLite database."""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_conn = sqlite3.connect(str(dest_path))
    try:
        src._conn().backup(dest_conn)
        dest_conn.commit()
    finally:
        dest_conn.close()
