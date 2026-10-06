"""Command line entry point."""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from . import config
from .alerts import check_and_notify, render
from .blocks import compute_blocks, status
from .collector import collect
from .db import connect
from .settings import load_spike_params
from .spikes import cache_write_spikes, detect, merge_runs, rate_spikes, suggest_floor


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
    sub.add_parser("status", help="current 5-hour window, speed and projection")
    p = sub.add_parser("blocks", help="5-hour windows")
    p.add_argument("--since", help="YYYY-MM-DD (local)")
    p = sub.add_parser("spikes", help="detected spikes (thresholds from ~/.moni_token/config.toml)")
    p.add_argument("--since", help="YYYY-MM-DD (local)")
    p.add_argument("--until", help="YYYY-MM-DD (local, exclusive)")
    p.add_argument("--all", action="store_true", help="show every hit, without re-alert suppression")
    sub.add_parser("check", help="collect, then notify about new spikes (for the scheduled task)")
    sub.add_parser("suggest-floor", help="data-driven candidate for spike.floor_usd (p75 of active 15-min windows, 7 days)")
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
    elif a.cmd == "status":
        s = status(con, now_ms())
        print(f"speed (15 min): ${s['rate_usd_per_min'] * 60:.2f}/h")
        b = s["block"]
        if b:
            print(f"5h window {fmt(b['start_ms'])} ~ {fmt(b['end_ms'])} ({b['source']}): ${b['usd']:.2f} so far, "
                  f"{b['calls']} calls, projected ${b['projected_usd']:.2f} at window end")
        else:
            print("no active 5h window")
    elif a.cmd == "blocks":
        for b in compute_blocks(con, day_ms(a.since) if a.since else 0):
            print(f"{fmt(b.start_ms)} ~ {fmt(b.end_ms)}  {b.source:9}  ${b.usd:>8.2f}  calls {b.calls}")
    elif a.cmd == "spikes":
        p = load_spike_params()
        start = day_ms(a.since) if a.since else 0
        end = day_ms(a.until) if a.until else now_ms()
        if a.all:
            hits = merge_runs(rate_spikes(con, start, end, p)) + cache_write_spikes(con, start, end, p)
        else:
            hits = detect(con, start, end, p)
        for s in sorted(hits, key=lambda x: x.start_ms):
            ratio = f"x{s.ratio:.1f}" if s.ratio else "x-"
            print(f"{fmt(s.start_ms)} ~ {fmt(s.end_ms)}  {s.kind:11} ${s.usd:>7.2f}  base ${s.baseline_usd:.2f} "
                  f"{ratio:>6}  {s.scope[:8]}  {json.dumps(s.evidence)}")
    elif a.cmd == "check":
        collect(con, a.projects or config.claude_projects_dir())
        for s in check_and_notify(con, now_ms(), load_spike_params()):
            print(" / ".join(render(s)))
    elif a.cmd == "suggest-floor":
        v = suggest_floor(con, now_ms())
        print("no data" if v is None else f"p75 of active 15-min windows (7 days): ${v:.2f}")
    return 0


def now_ms() -> int:
    return int(time.time() * 1000)


def day_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000)


def fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")


if __name__ == "__main__":
    raise SystemExit(main())
