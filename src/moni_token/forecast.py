"""Progress forecast from past runs (issue #7, ADR-0005). Empirical quantiles only — no model, no LLM.

A running subagent that has made `c` calls is compared with finished runs of the same agentType that also got at
least `c` calls; their totals give P50/P85 for calls, duration and usage. Below MIN_SAMPLES we say so instead.
"""
import math
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from .units import call_usd

MIN_SAMPLES = 10
RECENT_RUNS = 200
MIN_DONE_STEPS = 3
CHECK = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+")


def quantile(xs: list[float], q: float) -> float:
    """Nearest-rank quantile: deterministic and always one of the observed values."""
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))]


def runs(con: sqlite3.Connection, agent_type: str, project_dir: str | None = None,
         before_ms: int | None = None) -> list[dict]:
    """Finished runs of one agentType: total calls, duration and usage, newest first (at most RECENT_RUNS)."""
    q = ("SELECT c.session_id, c.agent_id, c.ts_ms, c.model, c.input, c.output, c.cache_read, c.cache_5m, c.cache_1h, "
         "c.web_search_n FROM calls c JOIN agent_meta m ON m.session_id = c.session_id AND m.agent_id = c.agent_id "
         "WHERE m.finished = 1 AND m.agent_type = ?")
    args: list = [agent_type]
    if project_dir:
        q += " AND m.project_dir = ?"
        args.append(project_dir)
    per: dict[tuple, dict] = {}
    for sid, aid, ts, model, inp, out, cr, c5, c1, web in con.execute(q, args):
        r = per.setdefault((sid, aid), dict(calls=0, first=ts, last=ts, usd=0.0))
        r["calls"] += 1
        r["first"], r["last"] = min(r["first"], ts), max(r["last"], ts)
        r["usd"] += call_usd(model, inp, out, cr, c5, c1, web)
    out = [dict(r, dur=r["last"] - r["first"], key=k) for k, r in per.items()
           if before_ms is None or r["last"] < before_ms]
    return sorted(out, key=lambda r: -r["last"])[:RECENT_RUNS]


def forecast_agent(con: sqlite3.Connection, agent_type: str, project_dir: str, calls: int, elapsed_ms: int,
                   usd: float, before_ms: int | None = None) -> dict:
    """P50/P85 of the run's total calls / duration / usage, given how far it already got."""
    f = forecast_from(runs(con, agent_type, project_dir, before_ms), calls, elapsed_ms, usd)
    if f["ok"]:
        return dict(f, scope="project")
    # not enough in this project: the same agentType anywhere
    return dict(forecast_from(runs(con, agent_type, None, before_ms), calls, elapsed_ms, usd), scope="all")


def forecast_from(history: list[dict], calls: int, elapsed_ms: int, usd: float) -> dict:
    hist = [r for r in history if r["calls"] >= calls]
    if len(hist) < MIN_SAMPLES:
        return dict(ok=False, n=len(hist))
    tot = [r["calls"] for r in hist]
    dur = [max(r["dur"], elapsed_ms) for r in hist]
    use = [max(r["usd"], usd) for r in hist]
    p = lambda xs: (quantile(xs, .5), quantile(xs, .85))
    c50, c85 = p(tot)
    d50, d85 = p(dur)
    u50, u85 = p(use)
    return dict(ok=True, n=len(hist), calls=calls, calls_p50=c50, calls_p85=c85,
                progress=round(min(0.99, calls / c50), 2) if c50 else None,
                remain_ms_p50=max(0, d50 - elapsed_ms), remain_ms_p85=max(0, d85 - elapsed_ms),
                remain_usd_p50=max(0.0, u50 - usd), remain_usd_p85=max(0.0, u85 - usd))


def remaining_usage(agent_forecasts: list[dict], usd_per_pct: float | None, five_hour_pct: float | None) -> dict | None:
    """Sum of what the running agents will probably still use, in %p of the 5-hour limit."""
    fs = [f for f in agent_forecasts if f.get("ok")]
    if not fs or not usd_per_pct:
        return None
    p50 = sum(f["remain_usd_p50"] for f in fs) / usd_per_pct
    p85 = sum(f["remain_usd_p85"] for f in fs) / usd_per_pct
    over = five_hour_pct is not None and five_hour_pct + p85 >= 100
    return dict(agents=len(fs), pp_p50=round(p50, 1), pp_p85=round(p85, 1), over_limit_p85=over)


def checklist_counts(path: Path) -> tuple[int, int] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    marks = [m.group(1) for m in map(CHECK.match, lines) if m]
    return (sum(x in "xX" for x in marks), len(marks)) if marks else None


def snapshot_checklists(con: sqlite3.Connection, roots: list[str], now_ms: int,
                        globs=("docs/PLAN.md", "PLAN.md", "SPEC.md", "docs/SPEC.md", "docs/tasks/*.md")) -> int:
    """Record (done, total) of each contract document when it changed since the last snapshot (ADR-0005)."""
    n = 0
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for g in globs:
            for doc in sorted(base.glob(g)):
                c = checklist_counts(doc)
                if not c:
                    continue
                rel = doc.relative_to(base).as_posix()
                last = con.execute("SELECT done, total FROM checklist_snaps WHERE root=? AND doc=? "
                                   "ORDER BY ts_ms DESC LIMIT 1", (root, rel)).fetchone()
                if last != c:
                    con.execute("INSERT OR REPLACE INTO checklist_snaps VALUES (?,?,?,?,?)", (root, rel, now_ms, *c))
                    n += 1
    con.commit()
    return n


def forecast_checklist(con: sqlite3.Connection, root: str, doc: str, now_ms: int) -> dict:
    """P50/P85 time to finish the remaining items, from the intervals between past completions."""
    snaps = con.execute("SELECT ts_ms, done, total FROM checklist_snaps WHERE root=? AND doc=? ORDER BY ts_ms",
                        (root, doc)).fetchall()
    if not snaps:
        return dict(ok=False, steps=0)
    gaps, prev = [], None
    for ts, done, _ in snaps:
        if prev and done > prev[1]:
            gaps += [(ts - prev[0]) / (done - prev[1])] * (done - prev[1])   # per completed item
        if not prev or done != prev[1]:
            prev = (ts, done)
    ts, done, total = snaps[-1]
    left = total - done
    if left <= 0:
        return dict(ok=True, done=done, total=total, left=0, steps=len(gaps))
    if len(gaps) < MIN_DONE_STEPS:
        return dict(ok=False, done=done, total=total, left=left, steps=len(gaps))
    return dict(ok=True, done=done, total=total, left=left, steps=len(gaps),
                eta_p50=now_ms + int(left * quantile(gaps, .5)), eta_p85=now_ms + int(left * quantile(gaps, .85)))


CHECKPOINTS = (1, 2, 4, 8, 16, 32, 64, 128)


def backtest(con: sqlite3.Connection) -> dict:
    """Replay the live situation: at fixed call counts (1, 2, 4, …) every run that got that far is forecast from runs
    that ended before it started; count how often its real total landed within P50 / P85.

    (Checkpoints as a share of each run's own length would leak the answer: a run checked at half its calls always
    ends at twice the checkpoint.)"""
    types = [t for (t,) in con.execute("SELECT DISTINCT agent_type FROM agent_meta WHERE finished=1 AND agent_type IS NOT NULL")]
    hits = defaultdict(lambda: [0, 0, 0])   # cases, total <= P85, total <= P50
    for t in types:   # all projects pooled
        allruns = sorted(runs(con, t, None), key=lambda r: r["last"])
        for r in allruns:
            earlier = [x for x in allruns if x["last"] < r["first"]][-RECENT_RUNS:]
            for c in CHECKPOINTS:
                if c > r["calls"]:
                    break
                f = forecast_from(earlier, c, 0, 0.0)
                if not f["ok"]:
                    continue
                h = hits[t]
                h[0] += 1
                h[1] += r["calls"] <= f["calls_p85"]
                h[2] += r["calls"] <= f["calls_p50"]
    total = sum(v[0] for v in hits.values())
    inside = sum(v[1] for v in hits.values())
    half = sum(v[2] for v in hits.values())
    return dict(cases=total, inside_p85=inside, coverage=round(inside / total, 3) if total else None,
                coverage_p50=round(half / total, 3) if total else None,
                by_type={t: dict(cases=v[0], coverage=round(v[1] / v[0], 3), coverage_p50=round(v[2] / v[0], 3))
                         for t, v in hits.items() if v[0]})
