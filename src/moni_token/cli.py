"""Command line entry point."""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from . import config
from .collector import collect
from .db import connect


def daily_totals(con, since: str | None = None) -> list[dict]:
    """Token totals per local calendar date (same grouping as `ccusage daily`)."""
    out: dict[str, dict] = {}
    for ts_ms, inp, outp, cr, c5, c1 in con.execute(
            "SELECT ts_ms, input, output, cache_read, cache_5m, cache_1h FROM calls"):
        day = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
        if since and day < since:
            continue
        d = out.setdefault(day, dict(date=day, input=0, output=0, cache_read=0, cache_write=0, calls=0))
        d["input"] += inp; d["output"] += outp; d["cache_read"] += cr; d["cache_write"] += c5 + c1; d["calls"] += 1
    return [out[k] for k in sorted(out)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="moni-token")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--projects", type=Path, default=None, help="Claude Code projects dir (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect", help="incrementally read new log lines into the database")
    p = sub.add_parser("daily", help="token totals per local date")
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    con = connect(a.db or config.db_path())
    if a.cmd == "collect":
        t0 = time.perf_counter()
        s = collect(con, a.projects or config.claude_projects_dir())
        print(f"files {s.files_seen} (read {s.files_read}), lines {s.lines}, call lines {s.call_lines}, "
              f"{s.bytes_read / 1e6:.1f} MB in {time.perf_counter() - t0:.1f}s")
    elif a.cmd == "daily":
        rows = daily_totals(con, a.since)
        if a.json:
            print(json.dumps(rows))
        else:
            for r in rows:
                print(f"{r['date']}  calls {r['calls']:>6}  in {r['input']:>10}  out {r['output']:>10}  "
                      f"cache_w {r['cache_write']:>12}  cache_r {r['cache_read']:>14}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
