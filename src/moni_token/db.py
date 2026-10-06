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
CREATE TABLE IF NOT EXISTS call_tools (
    msg_id TEXT NOT NULL,
    tool_use_id TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (msg_id, tool_use_id)
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con
