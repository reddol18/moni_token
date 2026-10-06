"""Command line entry point."""
import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import config
from .alerts import check_and_notify, render
from .analysis import analyze
from .blocks import compute_blocks, status
from .calibrate import add_manual, add_usage_reading, estimate, ingest_statusline
from .chunks import analyze_chunks
from .collector import collect
from .db import connect
from .events import list_events, record, record_spike
from .report import report_events, write_report
from .summary import digest
from .settings import load_spike_params
from .spikes import cache_write_spikes, detect, merge_runs, rate_spikes, suggest_floor


def collect_all(con, projects):
    s = collect(con, projects)
    ingest_statusline(con, config.data_dir() / "statusline.jsonl")
    return s


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
    sub.add_parser("check", help="collect, notify about new spikes, refresh the HTML report (for the scheduled task)")
    p = sub.add_parser("report", help="write the static HTML report (~/.moni_token/report.html)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--out", type=Path)
    p = sub.add_parser("analyze", help="explain a window you choose, e.g. --from '2026-10-06 12:31' --to '2026-10-06 12:56'")
    p.add_argument("--from", dest="frm", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--save", action="store_true", help="store as a manual event")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("events", help="recorded incidents")
    p.add_argument("--since", help="YYYY-MM-DD (local)")
    p = sub.add_parser("chunks", help="raw chunks shipped to the API per agent, and their share of tokens")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--min-tokens", type=int, default=5000, help="a tool result this big counts as a raw chunk")
    p = sub.add_parser("savings", help="what could have been saved, by cause (default: last 7 days)")
    p.add_argument("--since", help="YYYY-MM-DD (local)")
    p = sub.add_parser("backfill-events", help="detect and record past spikes (no notifications)")
    p.add_argument("--since", help="YYYY-MM-DD (local)")
    sub.add_parser("suggest-floor", help="data-driven candidate for spike.floor_usd (p75 of active 15-min windows, 7 days)")
    p = sub.add_parser("calibrate", help="add an observed limit usage (e.g. --from '2026-10-06 15:06' --to '2026-10-06 15:56' --pct 5) or show the estimate")
    p.add_argument("--from", dest="frm")
    p.add_argument("--to")
    p.add_argument("--pct", type=float)
    p = sub.add_parser("observe", help="enter what /usage shows, e.g. --session 49 --session-reset '2026-10-07 02:30' "
                                       "--week 7 --week-reset '2026-10-13 18:00'")
    p.add_argument("--session", type=float)
    p.add_argument("--session-reset")
    p.add_argument("--week", type=float)
    p.add_argument("--week-reset")
    p = sub.add_parser("statusline", help="status line command for Claude Code (records numbers only)")
    p.add_argument("--wrap", help="previous status line command to run and display")
    a = ap.parse_args(argv)

    if a.cmd == "statusline":   # hot path: no database
        from .statusline import main as sl_main
        return sl_main(["--wrap", a.wrap] if a.wrap else [])
    con = connect(a.db or config.db_path())
    if a.cmd == "collect":
        t0 = time.perf_counter()
        s = collect_all(con, a.projects or config.claude_projects_dir())
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
        e = estimate(con)
        if e and e["usd_per_pct"]:
            print(f"calibration: 1% of 5h limit ~ ${e['usd_per_pct']:.2f} (IQR ${e['q1']:.2f}~${e['q3']:.2f}, n={e['n']})")
        else:
            print(f"calibration: {e['n'] if e else 0} samples (need 3)")
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
        t0, c0 = time.perf_counter(), time.process_time()
        collect_all(con, a.projects or config.claude_projects_dir())
        for s in check_and_notify(con, now_ms(), load_spike_params()):
            print(" / ".join(render(s)))
        write_report(con, config.data_dir() / "report.html")
        with open(config.data_dir() / "runs.jsonl", "a", encoding="utf-8") as f:   # M5: resident cost
            f.write(json.dumps(dict(ts_ms=now_ms(), wall_ms=round((time.perf_counter() - t0) * 1000),
                                    cpu_ms=round((time.process_time() - c0) * 1000))) + "\n")
    elif a.cmd == "observe":
        collect_all(con, a.projects or config.claude_projects_dir())
        for kind, pct, reset, label in (("five_hour", a.session, a.session_reset, "current session"),
                                        ("seven_day", a.week, a.week_reset, "current week")):
            if pct is None or not reset:
                continue
            s = add_usage_reading(con, kind, pct, local_ms(reset), now_ms())
            per = f" -> ${s['usd'] / pct:.2f} per 1%" if pct else ""
            print(f"{label}: {pct}% since {fmt(s['start_ms'])} = ${s['usd']:.2f} in local logs{per}")
        for kind in ("five_hour", "seven_day"):
            e = estimate(con, kind, min_samples=1)
            print(f"{kind}: samples {e['n']}, median ${e['usd_per_pct'] or 0:.2f} per 1%")
        rebuild_events_since = now_ms() - 7 * 86_400_000
        con.execute("DELETE FROM events WHERE start_ms >= ?", (rebuild_events_since,))
        for s in detect(con, rebuild_events_since, now_ms(), load_spike_params()):
            record_spike(con, s, now_ms())
        write_report(con, config.data_dir() / "report.html")
    elif a.cmd == "calibrate":
        if a.frm and a.to and a.pct:
            s = add_manual(con, local_ms(a.frm), local_ms(a.to), a.pct, now_ms())
            print(f"saved: {a.frm} ~ {a.to}  {a.pct}%  = ${s['usd']:.2f} in local logs -> ${s['usd'] / a.pct:.2f} per 1%")
        e = estimate(con)
        for s in e["samples"]:
            print(f"  {fmt(s['start_ms'])}~{fmt(s['end_ms'])} {s['source']:10} {s['pct']:.1f}%  ${s['usd']:.2f}")
        print(f"estimate: ${e['usd_per_pct']:.2f} per 1% (n={e['n']})" if e["usd_per_pct"]
              else f"estimate: need {3 - e['n']} more sample(s)")
    elif a.cmd == "report":
        print(write_report(con, a.out or config.data_dir() / "report.html", days=a.days))
    elif a.cmd == "analyze":
        an = analyze(con, local_ms(a.frm), local_ms(a.to))
        if a.save:
            record(con, "manual", an, now_ms())
        if a.json:
            print(json.dumps(dict(start_ms=an.start_ms, end_ms=an.end_ms, usd=an.usd, calls=an.calls,
                                  sessions=an.sessions, causes=[asdict(c) for c in an.causes], note=an.note),
                             ensure_ascii=False))
        else:
            print_analysis(an)
    elif a.cmd == "events":
        evs = report_events(con, day_ms(a.since) if a.since else 0)
        for e in evs:
            rise = f" +{e['rise']}%p" if e.get("rise") else ""
            print(f"#{e['id']} {fmt(e['start'])}~{fmt(e['end'])} [{e['kind']}{rise}] {e['headline']}")
            print(f"    절약: {e['summary']}")
    elif a.cmd == "chunks":
        r = analyze_chunks(con, now_ms() - a.days * 86_400_000, now_ms(), a.min_tokens)
        o = r["overall"]
        print(f"최근 {a.days}일, 도구 결과 {a.min_tokens:,}토큰 이상을 '큰 덩어리'로 집계: 전체 {o['chunks']}건, "
              f"전체 토큰의 {o['token_share']:.1%} (비용 가중 {o['cost_share']:.1%})")
        print(f"{'에이전트':<18}{'호출':>7}{'덩어리':>7}{'/100호출':>9}{'중앙값':>9}{'토큰%':>8}{'비용%':>8}  주된 출처")
        for g in r["agents"]:
            if not g["chunks"]:
                continue
            top = ", ".join(f"{c['category']} {c['chunks']}건" for c in g["categories"][:3])
            print(f"{g['project'][:17]:<18}{g['calls']:>7}{g['chunks']:>7}{g['per_100_calls']:>9}{g['median_chunk']:>9,}"
                  f"{g['token_share']:>8.1%}{g['cost_share']:>8.1%}  {top}")
    elif a.cmd == "savings":
        evs = [e for e in report_events(con, day_ms(a.since) if a.since else now_ms() - 7 * 86_400_000)
               if e["kind"] == "pct"]
        d = digest(evs, estimate(con, "five_hour")["usd_per_pct"], estimate(con, "seven_day")["usd_per_pct"])
        print(d["headline"])
        for line in d["lines"]:
            print("  - " + line)
    elif a.cmd == "backfill-events":
        p = load_spike_params()
        n = 0
        for s in detect(con, day_ms(a.since) if a.since else 0, now_ms(), p):
            record_spike(con, s, now_ms())
            n += 1
        print(f"recorded {n} events")
    elif a.cmd == "suggest-floor":
        v = suggest_floor(con, now_ms())
        print("no data" if v is None else f"p75 of active 15-min windows (7 days): ${v:.2f}")
    return 0


def print_analysis(an) -> None:
    print(f"{fmt(an.start_ms)} ~ {fmt(an.end_ms)}  ${an.usd:.2f}, {an.calls} calls — {an.headline()}")
    for s in an.sessions[:5]:
        print(f"  session {s['session_id'][:8]} {s['project']:<16} {s['calls']:>4} calls  ${s['usd']:.2f} ({s['share']:.0%})")
    for c in an.causes:
        print(f"  [{c.label}] ${c.usd:.2f} {c.scope[:8]}")
        print(f"     사실: {c.fact_text}")
        print(f"     해석: {c.interpretation}")
        print(f"     권고: {c.advice}")
    print(f"  * {an.note}")


def local_ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp() * 1000)


def now_ms() -> int:
    return int(time.time() * 1000)


def day_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000)


def fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")


if __name__ == "__main__":
    raise SystemExit(main())
