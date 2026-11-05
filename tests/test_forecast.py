import json
import time

from conftest import SECRET
from moni_token import agentmap, forecast
from moni_token.collector import collect
from test_agentmap import _call, iso, project  # noqa: F401  (project is a fixture)


def test_quantile_is_nearest_rank():
    xs = [5, 1, 3, 2, 4]
    assert forecast.quantile(xs, .5) == 3 and forecast.quantile(xs, .85) == 5 and forecast.quantile(xs, 0) == 1


def test_forecast_conditions_on_progress_and_needs_samples():
    hist = [dict(calls=n, dur=n * 1000, usd=n * 0.1) for n in [2] * 30 + list(range(10, 20))]
    early = forecast.forecast_from(hist, 1, 0, 0.0)
    late = forecast.forecast_from(hist, 10, 5000, 0.5)
    assert early["ok"] and early["calls_p50"] == 2                 # most runs are short
    assert late["ok"] and late["n"] == 10 and late["calls_p50"] == 14   # only runs that got this far count
    assert late["remain_ms_p50"] == 14 * 1000 - 5000
    assert forecast.forecast_from(hist, 15, 0, 0.0) == dict(ok=False, n=5)   # below MIN_SAMPLES


def _runs(logs, root, n_runs, calls, agent_type="Explore", t0=None, session="s1", finished=True, desc=SECRET):
    t0 = t0 or int(time.time() * 1000) - 10 * 86_400_000
    for k in range(n_runs):
        aid = f"{agent_type[:2]}{k}"
        p = logs.path(session=session, agent=aid)
        logs.write(p, *[_call(f"{aid}m{i}", iso(t0 + k * 3_600_000 + i * 10_000), root, [], session=session, agent=aid)
                        for i in range(calls)])
        p.with_name(f"agent-{aid}.meta.json").write_text(
            json.dumps({"agentType": agent_type, "finished": finished, "description": desc}), encoding="utf-8")
    return t0


def test_running_subagent_gets_a_forecast_from_past_runs(logs, con, project):
    r = str(project)
    _runs(logs, r, 12, 8)                                            # 12 finished Explore runs of 8 calls
    now = int(time.time() * 1000)
    p = logs.path(session="s2", agent="live")
    logs.write(p, *[_call(f"live{i}", iso(now - 60_000 + i * 10_000), r, [], session="s2", agent="live") for i in range(3)])
    p.with_name("agent-live.meta.json").write_text(json.dumps({"agentType": "Explore", "finished": False}), encoding="utf-8")
    collect(con, logs.root)
    dump = "\n".join(str(x) for x in con.execute("SELECT * FROM agent_meta"))
    assert SECRET not in dump and "Explore" in dump                 # kind stored, description not
    st = agentmap.build_state(con, logs.root, now)
    live = next(a for a in st["agents"] if a["agent"] == "live")
    f = live["forecast"]
    assert f["ok"] and f["n"] == 12 and f["calls"] == 3 and f["calls_p50"] == 8 and f["progress"] == 0.38
    u = forecast.remaining_usage([f], usd_per_pct=f["remain_usd_p85"] / 10 or 1, five_hour_pct=95)
    assert u["agents"] == 1 and u["over_limit_p85"]


def test_checklist_snapshots_and_eta(con, tmp_path):
    root = tmp_path / "p"
    (root / "docs").mkdir(parents=True)
    plan = root / "docs" / "PLAN.md"
    t, H = 1_790_000_000_000, 3_600_000
    for i, done in enumerate([0, 1, 1, 2, 3, 4]):
        plan.write_text("\n".join(["- [x] a"] * done + ["- [ ] b"] * (8 - done)) + "\n" + SECRET, encoding="utf-8")
        forecast.snapshot_checklists(con, [str(root)], t + i * H)
    rows = con.execute("SELECT done, total FROM checklist_snaps").fetchall()
    assert rows == [(0, 8), (1, 8), (2, 8), (3, 8), (4, 8)]          # unchanged counts are not recorded twice
    assert SECRET not in str(con.execute("SELECT * FROM checklist_snaps").fetchall())
    f = forecast.forecast_checklist(con, str(root), "docs/PLAN.md", t + 6 * H)
    assert f["ok"] and f["left"] == 4 and f["steps"] == 4
    assert f["eta_p50"] == t + 6 * H + 4 * H                        # 1 item per hour, 4 left


def test_backtest_uses_only_earlier_runs(logs, con, project):
    r = str(project)
    _runs(logs, r, 25, 6)
    collect(con, logs.root)
    b = forecast.backtest(con)
    # the first 10 runs have fewer than 10 predecessors: no forecast for them
    assert b["cases"] == 15 * 3 and b["coverage"] == 1.0           # checkpoints 1, 2, 4 of each 6-call run
