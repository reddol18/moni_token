"""Record incidents: a spike (or a user-chosen window) plus its cause analysis."""
import json
import sqlite3
from dataclasses import asdict

from .analysis import Analysis, CauseParams, analyze
from .spikes import Spike

CACHE_WRITE_WINDOW_MS = 15 * 60_000


def spike_window(s: Spike) -> tuple[int, int]:
    if s.kind == "cache_write":  # the rewrite plus the calls that followed it
        return s.start_ms, s.start_ms + CACHE_WRITE_WINDOW_MS
    return s.start_ms, s.end_ms


def record(con: sqlite3.Connection, kind: str, a: Analysis, now_ms: int, spike: Spike | None = None) -> int:
    cur = con.execute(
        "INSERT INTO events (kind, start_ms, end_ms, created_ms, usd, baseline_usd, ratio, headline, causes_json, "
        "sessions_json, spike_json, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(kind, start_ms, end_ms) DO UPDATE SET usd=excluded.usd, headline=excluded.headline, "
        "causes_json=excluded.causes_json, sessions_json=excluded.sessions_json, created_ms=excluded.created_ms "
        "RETURNING id",
        (kind, a.start_ms, a.end_ms, now_ms, a.usd, spike.baseline_usd if spike else None,
         spike.ratio if spike else None, a.headline(), json.dumps([asdict(c) for c in a.causes], ensure_ascii=False),
         json.dumps(a.sessions, ensure_ascii=False), json.dumps(asdict(spike)) if spike else None, a.note))
    (eid,) = cur.fetchone()
    con.commit()
    return eid


def record_spike(con: sqlite3.Connection, s: Spike, now_ms: int, p: CauseParams = CauseParams()) -> tuple[int, Analysis]:
    a = analyze(con, *spike_window(s), p)
    return record(con, s.kind, a, now_ms, s), a


def list_events(con: sqlite3.Connection, since_ms: int = 0) -> list[dict]:
    cols = "id kind start_ms end_ms usd baseline_usd ratio headline causes_json sessions_json note".split()
    out = []
    for r in con.execute(f"SELECT {','.join(cols)} FROM events WHERE start_ms >= ? ORDER BY start_ms", (since_ms,)):
        d = dict(zip(cols, r))
        d["causes"] = json.loads(d.pop("causes_json"))
        d["sessions"] = json.loads(d.pop("sessions_json"))
        out.append(d)
    return out
