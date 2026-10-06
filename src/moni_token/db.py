"""SQLite schema. Numbers and metadata only — no message text is ever stored (CLAUDE.md rule 1)."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    offset INTEGER NOT NULL,
    -- per-file parse state carried between incremental runs
    pending_trigger TEXT,
    pending_result_bytes INTEGER NOT NULL DEFAULT 0,
    pending_result_image INTEGER NOT NULL DEFAULT 0,
    pending_attach_bytes INTEGER NOT NULL DEFAULT 0,
    last_msg_id TEXT
);
CREATE TABLE IF NOT EXISTS calls (
    msg_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,            -- ISO-8601 UTC as logged
    ts_ms INTEGER NOT NULL,      -- epoch milliseconds
    session_id TEXT,
    project_dir TEXT NOT NULL,
    project TEXT,                -- last folder name of the logged cwd (display only)
    agent_id TEXT,
    is_sidechain INTEGER NOT NULL,
    entrypoint TEXT,
    model TEXT,
    input INTEGER NOT NULL,
    output INTEGER NOT NULL,
    cache_read INTEGER NOT NULL,
    cache_5m INTEGER NOT NULL,
    cache_1h INTEGER NOT NULL,
    thinking INTEGER NOT NULL,
    web_search_n INTEGER NOT NULL,
    web_fetch_n INTEGER NOT NULL,
    stop_reason TEXT,
    trigger TEXT,                -- human | tool_result | other
    prev_result_bytes INTEGER NOT NULL,
    prev_result_image INTEGER NOT NULL,
    prev_attach_bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts_ms);
CREATE INDEX IF NOT EXISTS calls_session ON calls(session_id, ts_ms);
-- known 5-hour/weekly window resets: from logs (quotaLimits), statusline recorder, or manual input
CREATE TABLE IF NOT EXISTS limit_obs (
    ts_ms INTEGER NOT NULL,
    source TEXT NOT NULL,        -- log | statusline | manual
    kind TEXT NOT NULL,          -- five_hour | seven_day | ...
    resets_at_ms INTEGER NOT NULL,
    status TEXT,
    used_pct REAL,
    PRIMARY KEY (source, kind, resets_at_ms, ts_ms)
);
-- 5-minute aggregates in usage units (list-price USD)
CREATE TABLE IF NOT EXISTS buckets (
    t5_ms INTEGER NOT NULL,
    project_dir TEXT NOT NULL,
    session_id TEXT NOT NULL,
    calls INTEGER NOT NULL,
    input INTEGER NOT NULL, output INTEGER NOT NULL, cache_read INTEGER NOT NULL,
    cache_5m INTEGER NOT NULL, cache_1h INTEGER NOT NULL,
    usd REAL NOT NULL,
    sidechain_calls INTEGER NOT NULL,
    PRIMARY KEY (t5_ms, project_dir, session_id)
);
CREATE TABLE IF NOT EXISTS alerts (
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    alerted_ms INTEGER NOT NULL,
    delivered INTEGER NOT NULL,
    PRIMARY KEY (kind, scope, start_ms)
);
-- incidents with their evidence; text columns hold template sentences only, never log content
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,          -- rate | cache_write | manual
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    created_ms INTEGER NOT NULL,
    usd REAL NOT NULL,
    baseline_usd REAL,
    ratio REAL,
    headline TEXT NOT NULL,
    causes_json TEXT NOT NULL,
    sessions_json TEXT NOT NULL,
    spike_json TEXT,
    note TEXT,
    UNIQUE (kind, start_ms, end_ms)
);
CREATE TABLE IF NOT EXISTS call_tools (
    msg_id TEXT NOT NULL,
    tool_use_id TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (msg_id, tool_use_id)
);
"""


SCHEMA_VERSION = 3


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the DB. Everything in it is derived from the logs, so a schema change simply rebuilds it
    (the next `collect` re-reads the logs from offset 0)."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    (ver,) = con.execute("PRAGMA user_version").fetchone()
    if ver != SCHEMA_VERSION:
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            con.execute(f'DROP TABLE "{name}"')
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.executescript(SCHEMA)
    return con
