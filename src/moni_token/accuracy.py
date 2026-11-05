"""Forecast accuracy (issue #9): record the forecasts shown live, score them once the run finished.

Scoring follows the backtest (ADR-0005): a forecast "hit P50" when the real total calls were <= its P50, "hit P85"
when <= its P85 (the forecast is conditional on the calls so far, so only the upper side can miss). Numbers only.
"""
import sqlite3
from collections import defaultdict
from datetime import datetime

from .agentmap import run_so_far
from .forecast import backtest_cases, checklist_from, forecast_agent

ACTIVE_MS = 10 * 60_000        # a subagent with a call this recent is "running" for recording
UNRESOLVED_MS = 3_600_000      # not finished and silent this long: the session was closed, never scored as a miss
MIN_CASES = 20
SESSIONS_SHOWN = 30


def record_live(con: sqlite3.Connection, now_ms: int) -> int:
    """Store the forecast of every running subagent, once per call count it reached."""
    n = 0
    rows = con.execute(
        "SELECT m.session_id, m.agent_id, m.project_dir, m.agent_type FROM agent_meta m WHERE m.finished = 0 "
        "AND EXISTS (SELECT 1 FROM calls c WHERE c.session_id = m.session_id AND c.agent_id = m.agent_id "
        "AND c.ts_ms >= ?)", (now_ms - ACTIVE_MS,)).fetchall()
    for sid, aid, pd, typ in rows:
        calls, first, usd = run_so_far(con, sid, aid, now_ms)
        if not calls or con.execute("SELECT 1 FROM forecast_snaps WHERE session_id=? AND agent_id=? AND calls=?",
                                    (sid, aid, calls)).fetchone():
            continue
        f = forecast_agent(con, typ or "subagent", pd, calls, now_ms - first, usd, before_ms=now_ms)
        if not f["ok"]:
            continue
        con.execute("INSERT OR IGNORE INTO forecast_snaps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, aid, pd, typ, now_ms, calls, now_ms - first, f["calls_p50"], f["calls_p85"],
                     f["remain_ms_p50"], f["remain_ms_p85"], f["n"], f["scope"]))
        n += 1
    con.commit()
    return n


def live_cases(con: sqlite3.Connection, now_ms: int) -> tuple[list[dict], dict]:
    """Recorded forecasts joined with how the run really ended. Returns (scored cases, counts of the others)."""
    actual = {}
    for sid, aid, n, first, last in con.execute(
            "SELECT c.session_id, c.agent_id, COUNT(*), MIN(c.ts_ms), MAX(c.ts_ms) FROM calls c "
            "WHERE EXISTS (SELECT 1 FROM forecast_snaps s WHERE s.session_id = c.session_id "
            "AND s.agent_id = c.agent_id) GROUP BY c.session_id, c.agent_id"):
        actual[(sid, aid)] = (n, first, last)
    finished = {(s, a): f for s, a, f in con.execute("SELECT session_id, agent_id, finished FROM agent_meta")}
    out, other = [], dict(pending=0, unresolved=0)
    for (sid, aid, pd, typ, ts, calls, el, p50, p85, r50, r85, n, scope) in con.execute(
            "SELECT * FROM forecast_snaps ORDER BY ts_ms"):
        tot, first, last = actual.get((sid, aid), (calls, ts, ts))
        if not finished.get((sid, aid)):
            other["unresolved" if now_ms - last > UNRESOLVED_MS else "pending"] += 1
            continue
        out.append(dict(session_id=sid, agent_id=aid, project_dir=pd, agent_type=typ or "subagent", ts_ms=ts,
                        end_ms=last, calls=calls, calls_p50=p50, calls_p85=p85, n=n, actual=tot,
                        remain_ms=max(0, last - ts), remain_ms_p50=r50, remain_ms_p85=r85))
    return out, other


def _agg(cases: list[dict]) -> dict:
    n = len(cases)
    if not n:
        return dict(n=0)
    c50 = sum(k["actual"] <= k["calls_p50"] for k in cases) / n
    c85 = sum(k["actual"] <= k["calls_p85"] for k in cases) / n
    err = sorted(abs(k["actual"] - k["calls_p50"]) for k in cases)
    d = dict(n=n, cov_p50=round(c50, 3), cov_p85=round(c85, 3), mae_calls=err[(n - 1) // 2],
             ratio=round(sorted(k["actual"] / k["calls_p50"] for k in cases)[(n - 1) // 2], 2),
             runs=len({(k["session_id"], k["agent_id"]) for k in cases}), verdict=calibration(n, c85))
    timed = [k for k in cases if "remain_ms" in k]
    if timed:
        te = sorted(abs(k["remain_ms"] - k["remain_ms_p50"]) for k in timed)
        d["mae_min"] = round(te[(len(te) - 1) // 2] / 60000, 1)
        d["cov_time_p85"] = round(sum(k["remain_ms"] <= k["remain_ms_p85"] for k in timed) / len(timed), 3)
    return d


def calibration(n: int, cov_p85: float) -> str:
    """Too few cases / ranges too wide (safe side) / about right / too narrow (misses more than 15%)."""
    if n < MIN_CASES:
        return "few"
    if cov_p85 > 0.92:
        return "wide"
    if cov_p85 < 0.78:
        return "narrow"
    return "ok"


def _week(ms: int) -> str:
    y, w, _ = datetime.fromtimestamp(ms / 1000).isocalendar()
    return f"{y}-W{w:02d}"


def summarize(con: sqlite3.Connection, cases: list[dict]) -> dict:
    names = dict(con.execute("SELECT project_dir, project FROM calls WHERE project IS NOT NULL GROUP BY project_dir"))
    pdir = dict(((s, a), p) for s, a, p in con.execute("SELECT session_id, agent_id, project_dir FROM agent_meta"))
    by_type, by_week, by_run = defaultdict(list), defaultdict(list), defaultdict(list)
    for k in cases:
        by_type[k["agent_type"]].append(k)
        by_week[_week(k["ts_ms"])].append(k)
        by_run[(k["session_id"], k["agent_id"])].append(k)
    sessions: dict[str, dict] = {}
    for (sid, aid), ks in by_run.items():
        pd = ks[0].get("project_dir") or pdir.get((sid, aid), "")
        s = sessions.setdefault(sid, dict(session=(sid or "")[:8], session_id=sid, runs=[], last=0,
                                          project=names.get(pd, pd)))
        ks.sort(key=lambda k: k["calls"])
        s["runs"].append(dict(agent=(aid or "")[:8], type=ks[0]["agent_type"], actual=ks[0]["actual"],
                              end_ms=ks[0]["end_ms"], hit_p85=all(k["actual"] <= k["calls_p85"] for k in ks),
                              points=[[k["calls"], k["calls_p50"], k["calls_p85"]] for k in ks]))
        s["last"] = max(s["last"], ks[0]["end_ms"])
    recent = sorted(sessions.values(), key=lambda s: -s["last"])[:SESSIONS_SHOWN]
    for s in recent:
        s["runs"].sort(key=lambda r: r["end_ms"])
        s.update({k: v for k, v in _agg([c for c in cases if c["session_id"] == s["session_id"]]).items()
                  if k != "runs"})   # keep the list of runs
    return dict(overall=_agg(cases),
                by_type=sorted(({"type": t, **_agg(ks)} for t, ks in by_type.items()), key=lambda d: -d["n"]),
                by_week=[{"week": w, "ts": min(k["ts_ms"] for k in by_week[w]), **_agg(by_week[w])}
                         for w in sorted(by_week)],
                sessions=recent, sessions_total=len(sessions))


def checklist_replay(con: sqlite3.Connection) -> list[dict]:
    """Documents whose checklist was finished: at each earlier snapshot, was the real finish within P50/P85?"""
    out = []
    for root, doc in con.execute("SELECT DISTINCT root, doc FROM checklist_snaps").fetchall():
        snaps = con.execute("SELECT ts_ms, done, total FROM checklist_snaps WHERE root=? AND doc=? ORDER BY ts_ms",
                            (root, doc)).fetchall()
        fin = next((s[0] for s in snaps if s[2] and s[1] >= s[2]), None)
        if not fin:
            continue
        hits = [0, 0, 0]
        for i in range(1, len(snaps)):
            if snaps[i - 1][0] >= fin:
                break
            f = checklist_from(snaps[:i], snaps[i - 1][0])
            if f.get("ok") and f.get("left"):
                hits[0] += 1
                hits[1] += fin <= f["eta_p50"]
                hits[2] += fin <= f["eta_p85"]
        out.append(dict(doc=doc, root_name=root.replace("\\", "/").rstrip("/").split("/")[-1], finished_ms=fin,
                        cases=hits[0], cov_p50=round(hits[1] / hits[0], 3) if hits[0] else None,
                        cov_p85=round(hits[2] / hits[0], 3) if hits[0] else None))
    return out


def build(con: sqlite3.Connection, now_ms: int, backtest: list[dict] | None = None) -> dict:
    live, other = live_cases(con, now_ms)
    bt = backtest if backtest is not None else backtest_cases(con)
    return dict(now=now_ms, live=dict(summarize(con, live), **other), backtest=summarize(con, bt),
                checklists=checklist_replay(con), min_cases=MIN_CASES)
