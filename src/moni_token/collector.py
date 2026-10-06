"""Incremental, read-only collection of Claude Code logs into SQLite (ADR-0001)."""
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .buckets import rebuild_buckets
from .parser import FileState, process_line

UPSERT_CALL = """
INSERT INTO calls ({cols}) VALUES ({ph})
ON CONFLICT(msg_id) DO UPDATE SET
    output = MAX(calls.output, excluded.output),
    stop_reason = COALESCE(excluded.stop_reason, calls.stop_reason)
"""
CALL_COLS = ("msg_id ts ts_ms session_id project_dir project agent_id is_sidechain entrypoint model input output "
             "cache_read cache_5m cache_1h thinking web_search_n web_fetch_n stop_reason trigger "
             "prev_result_bytes prev_result_image prev_attach_bytes").split()
_UPSERT = UPSERT_CALL.format(cols=",".join(CALL_COLS), ph=",".join("?" * len(CALL_COLS)))


@dataclass
class CollectStats:
    files_seen: int = 0
    files_read: int = 0
    bytes_read: int = 0
    lines: int = 0
    call_lines: int = 0
    min_ts_ms: int | None = None   # earliest call touched — buckets are rebuilt from here


def log_files(projects: Path) -> list[Path]:
    """Main sessions, subagents and workflow agents. Workflow journals carry no usage."""
    files = [p for p in projects.rglob("*.jsonl") if p.name != "journal.jsonl"]
    # oldest first: when a resumed session copies history, the original file claims the call
    return sorted(files, key=lambda p: (p.stat().st_mtime, str(p)))


def collect_file(con: sqlite3.Connection, path: Path, projects: Path, stats: CollectStats) -> None:
    st = path.stat()
    key = str(path)
    row = con.execute("SELECT size, mtime, offset, pending_trigger, pending_result_bytes, pending_result_image, "
                      "pending_attach_bytes, last_msg_id, pending_results_json FROM files WHERE path=?", (key,)).fetchone()
    state = FileState()
    last = [None]
    offset = 0
    if row:
        size, mtime, offset, *pend, last_id, pend_results = row
        if st.st_size == size and st.st_mtime == mtime:
            return
        if st.st_size < offset:  # replaced or truncated: start over (dedupe makes it safe)
            offset = 0
        else:
            state.trigger, state.result_bytes, state.result_image, state.attach_bytes = pend[0], pend[1], bool(pend[2]), pend[3]
            last[0] = last_id
            state.results = json.loads(pend_results) if pend_results else []
    project_dir = path.relative_to(projects).parts[0]
    stats.files_read += 1
    quotas: list[dict] = []
    compactions: list[dict] = []
    with open(path, "rb") as fh:  # read-only, never modified
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):  # partial line being written: leave for next run
                break
            offset += len(raw)
            stats.lines += 1
            rec = process_line(raw, project_dir, state, last, quotas, compactions)
            if rec is None:
                continue
            call, tools = rec
            stats.call_lines += 1
            if stats.min_ts_ms is None or call["ts_ms"] < stats.min_ts_ms:
                stats.min_ts_ms = call["ts_ms"]
            con.execute(_UPSERT, [call[c] for c in CALL_COLS])
            if tools:
                con.executemany("INSERT OR IGNORE INTO call_tools VALUES (?,?,?)",
                                [(call["msg_id"], tid, name) for tid, name in tools])
            if call["_results"]:
                con.executemany("INSERT OR IGNORE INTO tool_results VALUES (?,?,?,?)",
                                [(tid or f"{call['msg_id']}#{i}", call["msg_id"], size, img)
                                 for i, (tid, size, img) in enumerate(call["_results"])])
    con.executemany("INSERT OR IGNORE INTO compactions VALUES (:session_id, :agent_id, :ts_ms)", compactions)
    con.executemany("INSERT OR IGNORE INTO limit_obs (ts_ms, source, kind, resets_at_ms, status) "
                    "VALUES (:ts_ms, :source, :kind, :resets_at_ms, :status)", quotas)
    stats.bytes_read += offset - (row[2] if row and st.st_size >= row[2] else 0)
    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, st.st_size, st.st_mtime, offset, state.trigger, state.result_bytes,
                 int(state.result_image), state.attach_bytes, last[0], json.dumps(state.results or [])))


def collect(con: sqlite3.Connection, projects: Path) -> CollectStats:
    stats = CollectStats()
    if not projects.is_dir():
        return stats
    for path in log_files(projects):
        stats.files_seen += 1
        try:
            collect_file(con, path, projects, stats)
        except OSError:
            continue  # file vanished or locked; retry next run
        con.commit()
    if stats.min_ts_ms is not None:
        rebuild_buckets(con, stats.min_ts_ms)
    return stats
