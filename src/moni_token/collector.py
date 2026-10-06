"""Incremental, read-only collection of Claude Code logs into SQLite (ADR-0001)."""
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .parser import FileState, process_line

UPSERT_CALL = """
INSERT INTO calls ({cols}) VALUES ({ph})
ON CONFLICT(msg_id) DO UPDATE SET
    output = MAX(calls.output, excluded.output),
    stop_reason = COALESCE(excluded.stop_reason, calls.stop_reason)
"""
CALL_COLS = ("msg_id ts ts_ms session_id project_dir agent_id is_sidechain entrypoint model input output "
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


def log_files(projects: Path) -> list[Path]:
    """Main sessions, subagents and workflow agents. Workflow journals carry no usage."""
    files = [p for p in projects.rglob("*.jsonl") if p.name != "journal.jsonl"]
    # oldest first: when a resumed session copies history, the original file claims the call
    return sorted(files, key=lambda p: (p.stat().st_mtime, str(p)))


def collect_file(con: sqlite3.Connection, path: Path, projects: Path, stats: CollectStats) -> None:
    st = path.stat()
    key = str(path)
    row = con.execute("SELECT size, mtime, offset, pending_trigger, pending_result_bytes, pending_result_image, "
                      "pending_attach_bytes, last_msg_id FROM files WHERE path=?", (key,)).fetchone()
    state = FileState()
    last = [None]
    offset = 0
    if row:
        size, mtime, offset, *pend, last_id = row
        if st.st_size == size and st.st_mtime == mtime:
            return
        if st.st_size < offset:  # replaced or truncated: start over (dedupe makes it safe)
            offset = 0
        else:
            state.trigger, state.result_bytes, state.result_image, state.attach_bytes = pend[0], pend[1], bool(pend[2]), pend[3]
            last[0] = last_id
    project_dir = path.relative_to(projects).parts[0]
    stats.files_read += 1
    with open(path, "rb") as fh:  # read-only, never modified
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):  # partial line being written: leave for next run
                break
            offset += len(raw)
            stats.lines += 1
            rec = process_line(raw, project_dir, state, last)
            if rec is None:
                continue
            call, tools = rec
            stats.call_lines += 1
            con.execute(_UPSERT, [call[c] for c in CALL_COLS])
            if tools:
                con.executemany("INSERT OR IGNORE INTO call_tools VALUES (?,?,?)",
                                [(call["msg_id"], tid, name) for tid, name in tools])
    stats.bytes_read += offset - (row[2] if row and st.st_size >= row[2] else 0)
    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                (key, st.st_size, st.st_mtime, offset, state.trigger, state.result_bytes,
                 int(state.result_image), state.attach_bytes, last[0]))


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
    return stats
