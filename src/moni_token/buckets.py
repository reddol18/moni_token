"""5-minute aggregates in usage units, rebuilt incrementally from the earliest touched call."""
import sqlite3

from .units import call_usd

BUCKET_MS = 5 * 60 * 1000


def rebuild_buckets(con: sqlite3.Connection, since_ms: int = 0) -> None:
    t0 = since_ms - since_ms % BUCKET_MS
    agg: dict[tuple, list] = {}
    for (ts_ms, proj, sid, side, model, inp, out, cr, c5, c1, ws) in con.execute(
            "SELECT ts_ms, project_dir, COALESCE(session_id,''), is_sidechain, model, input, output, cache_read, "
            "cache_5m, cache_1h, web_search_n FROM calls WHERE ts_ms >= ?", (t0,)):
        k = (ts_ms - ts_ms % BUCKET_MS, proj, sid)
        a = agg.setdefault(k, [0, 0, 0, 0, 0, 0, 0.0, 0])
        a[0] += 1; a[1] += inp; a[2] += out; a[3] += cr; a[4] += c5; a[5] += c1
        a[6] += call_usd(model, inp, out, cr, c5, c1, ws)
        a[7] += side
    con.execute("DELETE FROM buckets WHERE t5_ms >= ?", (t0,))
    con.executemany("INSERT INTO buckets VALUES (?,?,?,?,?,?,?,?,?,?,?)", [(*k, *v) for k, v in agg.items()])
    con.commit()
