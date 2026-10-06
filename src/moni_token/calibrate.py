"""Limit-% calibration (ADR-0002 §3): "1% of the 5-hour limit ~= N usage-$".

Samples come from the status line recorder (automatic) or from the user (manual). The account's %
also moves for usage outside these logs (claude.ai web, other PCs), so the estimate is a median with
its quartile range, and it is hidden until there are enough samples.
"""
import json
import sqlite3
import statistics
from pathlib import Path

MIN_SAMPLES = 3
RECENT = 20
MIN_DELTA_PCT = 1.0


def ingest_statusline(con: sqlite3.Connection, path: Path) -> int:
    """Incrementally load statusline.jsonl into limit_obs (same offset bookkeeping as the logs)."""
    if not path.exists():
        return 0
    key = str(path)
    row = con.execute("SELECT offset FROM files WHERE path=?", (key,)).fetchone()
    offset = row[0] if row and row[0] <= path.stat().st_size else 0
    n = 0
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            for kind, pct, reset in (("five_hour", o.get("fh_pct"), o.get("fh_reset")),
                                     ("seven_day", o.get("sd_pct"), o.get("sd_reset"))):
                if pct is None or reset is None:
                    continue
                con.execute("INSERT OR IGNORE INTO limit_obs VALUES (?,?,?,?,?,?)",
                            (o["ts_ms"], "statusline", kind, int(reset * 1000 if reset < 1e12 else reset), "", pct))
                n += 1
    st = path.stat()
    con.execute("INSERT OR REPLACE INTO files (path, size, mtime, offset) VALUES (?,?,?,?)",
                (key, st.st_size, st.st_mtime, offset))
    con.commit()
    return n


def usd_between(con: sqlite3.Connection, a_ms: int, b_ms: int) -> float:
    from .units import call_usd
    return sum(call_usd(*r) for r in con.execute(
        "SELECT model, input, output, cache_read, cache_5m, cache_1h, web_search_n FROM calls "
        "WHERE ts_ms >= ? AND ts_ms < ?", (a_ms, b_ms)))


def statusline_samples(con: sqlite3.Connection, kind: str = "five_hour") -> list[dict]:
    """Consecutive observations inside one limit window whose % rose by >= MIN_DELTA_PCT."""
    obs = con.execute("SELECT ts_ms, resets_at_ms, used_pct FROM limit_obs WHERE source='statusline' "
                      "AND kind=? AND used_pct IS NOT NULL ORDER BY ts_ms", (kind,)).fetchall()
    out, anchor = [], None
    for ts, reset, pct in obs:
        if anchor is None or reset != anchor[1] or pct < anchor[2]:
            anchor = (ts, reset, pct)
            continue
        if pct - anchor[2] >= MIN_DELTA_PCT:
            usd = usd_between(con, anchor[0], ts)
            out.append(dict(start_ms=anchor[0], end_ms=ts, pct=pct - anchor[2], usd=usd, source="statusline"))
            anchor = (ts, reset, pct)
    return out


def manual_samples(con: sqlite3.Connection) -> list[dict]:
    return [dict(start_ms=a, end_ms=b, pct=p, usd=usd_between(con, a, b), source="manual")
            for a, b, p in con.execute("SELECT start_ms, end_ms, pct FROM calib_manual ORDER BY start_ms")]


def add_manual(con: sqlite3.Connection, start_ms: int, end_ms: int, pct: float, now_ms: int) -> dict:
    con.execute("INSERT OR REPLACE INTO calib_manual VALUES (?,?,?,?)", (start_ms, end_ms, pct, now_ms))
    con.commit()
    return dict(start_ms=start_ms, end_ms=end_ms, pct=pct, usd=usd_between(con, start_ms, end_ms))


def estimate(con: sqlite3.Connection, kind: str = "five_hour", min_samples: int = MIN_SAMPLES) -> dict:
    """Median "usage-$ per 1% of the limit". Manual observations describe the 5-hour window only.

    `reliable` is False below MIN_SAMPLES; callers that draw an estimate line pass min_samples=1 and
    label it with n so the reader can judge.
    """
    manual = manual_samples(con) if kind == "five_hour" else []
    samples = sorted(statusline_samples(con, kind) + manual, key=lambda s: s["end_ms"])[-RECENT:]
    ratios = [s["usd"] / s["pct"] for s in samples if s["pct"] > 0 and s["usd"] > 0]
    if len(ratios) < max(1, min_samples):
        return dict(usd_per_pct=None, n=len(ratios), samples=samples, reliable=False)
    q = statistics.quantiles(ratios, n=4) if len(ratios) >= 4 else [min(ratios), None, max(ratios)]
    return dict(usd_per_pct=statistics.median(ratios), q1=q[0], q3=q[-1], n=len(ratios), samples=samples,
                reliable=len(ratios) >= MIN_SAMPLES)
