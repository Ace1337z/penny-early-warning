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

SCHEMA_VERSION = 2

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


-- Per-alert tracking, from alert time to the close. Updated from the quotes the
-- engine already polls every cycle; no extra fetch. (high_after/low_after are a
-- running high/low over the quotes seen since the alert, which is what max gain,
-- drawdown and the stop/target tests actually need.)
CREATE TABLE IF NOT EXISTS alert_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    alert_ts REAL NOT NULL,
    alert_price REAL NOT NULL,
    primary_strategy TEXT,
    entry_trigger TEXT,
    gate_score REAL,
    ai_rating TEXT,
    ai_verdict TEXT,
    ai_key_findings TEXT,
    -- Spec 2.0 C/D: provenance of the decision block, so /stats can measure
    -- whether the AI earns its call: ai | ai_rejected | rules.
    ai_source TEXT,
    cause_present INTEGER DEFAULT 0,
    plan_entry_low REAL,
    plan_entry_high REAL,
    plan_stop REAL,
    plan_t1 REAL,
    plan_t2 REAL,
    high_after REAL,
    low_after REAL,
    last_price REAL,
    close_price REAL,
    max_gain_pct REAL,
    max_drawdown_pct REAL,
    close_pct REAL,
    hit_stop INTEGER DEFAULT 0,
    hit_t1 INTEGER DEFAULT 0,
    hit_t2 INTEGER DEFAULT 0,
    -- Feature 1: the quote reality captured at the alert, kept so a later
    -- review can tell a move that failed from one that was never fillable.
    bid_at_alert REAL,
    ask_at_alert REAL,
    spread_pct_at_alert REAL,
    dollar_vol_15m_at_alert REAL,
    avg_trade_size_at_alert REAL,
    -- Feature 6: the analog sample behind the ANALOGS line, if there was one.
    analog_t1_hit_rate REAL,
    analog_stop_hit_rate REAL,
    analog_median_peak_pct REAL,
    -- Feature 9 / 10: sector context and how fast the position could be out.
    sector_at_alert TEXT,
    sector_hot INTEGER,
    exit_liq_2k_seconds REAL,
    -- Feature 4: invalidation. `aborted` is a terminal status, distinct from
    -- `closed`, so an invalidated setup is still reported and still graded.
    aborted INTEGER DEFAULT 0,
    abort_ts REAL,
    abort_reason TEXT,
    -- Feature 5: the AI's confidence after the historical clamp, and whether
    -- the clamp fired. The raw number is in ai_calls; this is what was shown.
    ai_confidence_calibrated REAL,
    -- Feature 14: the post-mortem paragraph for a losing alert.
    postmortem_text TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    UNIQUE(symbol, day)
);

-- Completed sessions' OHLCV per symbol. Written from the same quote rows, so it
-- fills in as the system runs rather than backfilled. Without it the detectors
-- that read prior sessions (first green day, day-3 surge, WORG) have no prior
-- close to compare against and can only report INSUFFICIENT_DATA.
CREATE TABLE IF NOT EXISTS daily_close (
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    PRIMARY KEY (symbol, day)
);

CREATE TABLE IF NOT EXISTS suppressed (
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    tier INTEGER,
    reason TEXT,
    price REAL,
    pct REAL
);

-- Spec 1.3: fail-closed SEC vetos are logged here with their reason code so a
-- later review can tell an offering veto from a reverse-split veto without
-- trawling the logs.
CREATE TABLE IF NOT EXISTS screener_decisions (
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    detail TEXT,
    price REAL
);
CREATE INDEX IF NOT EXISTS idx_scr_dec_symbol_ts ON screener_decisions(symbol, ts);
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


def _declared_columns(schema: str) -> dict:
    """table -> [(column, definition)] as SQLite itself parses the schema.

    Run against a scratch in-memory database rather than a regex: the schema has
    `--` comments that contain commas, and a hand parser gets that wrong in
    ways that are invisible until a column goes missing on a live database.
    SQLite has already parsed it correctly by the time this returns.
    """
    out: dict = {}
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(schema)
        tables = [r[0] for r in scratch.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchall()]
        for table in tables:
            cols = []
            for row in scratch.execute("PRAGMA table_info(%s)" % table).fetchall():
                name = row[1]
                ctype = (row[2] or "").strip()
                default = row[4]
                # NOT NULL is deliberately dropped: SQLite refuses to add a NOT
                # NULL column without a default to a table that has rows, and
                # every added column here is genuinely optional.
                definition = ctype + (" DEFAULT %s" % default if default is not None else "")
                cols.append((name, definition.strip()))
            out[table] = cols
    except sqlite3.Error as exc:  # noqa: BLE001
        log.warning("could not read declared schema: %s", exc)
    finally:
        scratch.close()
    return out


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
                self._ensure_columns(conn)
                cur = conn.execute("SELECT COUNT(*) AS c FROM schema_version")
                if cur.fetchone()["c"] == 0:
                    conn.execute("INSERT INTO schema_version(version) VALUES (?)",
                                 (SCHEMA_VERSION,))
                conn.commit()
            self._initialized = True

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        """Add any column the current schema declares and the database lacks.

        SQLite has no `ADD COLUMN IF NOT EXISTS`, so each table is inspected
        first. A column that already exists is skipped, which is the normal path
        on a fresh database: re-running this must be harmless, because it runs on
        every open.
        """
        for table, cols in _declared_columns(self._schema).items():
            try:
                have = {r[1] for r in conn.execute(
                    "PRAGMA table_info(%s)" % table).fetchall()}
            except sqlite3.Error:
                continue
            if not have:
                continue                      # table is not in this database
            for column, definition in cols:
                if column in have or not definition:
                    continue
                try:
                    conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                                 % (table, column, definition))
                except sqlite3.Error as exc:  # noqa: BLE001
                    log.warning("could not add %s.%s: %s", table, column, exc)

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
