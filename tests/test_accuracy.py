import json
import time

from conftest import SECRET
from moni_token import accpage, accuracy
from moni_token.collector import collect
from test_agentmap import _call, iso, project  # noqa: F401  (project is a fixture)
from test_forecast import _runs


def _live(logs, root, n, now, finished=False, aid="live", session="s2"):
    p = logs.path(session=session, agent=aid)
    logs.write(p, *[_call(f"{aid}{i}", iso(now - 120_000 + i * 10_000), root, [], session=session, agent=aid)
                    for i in range(n)])
    p.with_name(f"agent-{aid}.meta.json").write_text(
        json.dumps({"agentType": "Explore", "finished": finished, "description": SECRET}), encoding="utf-8")


def test_live_forecasts_are_recorded_once_per_call_count_and_scored(logs, con, project):
    r = str(project)
    _runs(logs, r, 12, 8)                                    # history: 12 finished Explore runs of 8 calls
    now = int(time.time() * 1000)
    _live(logs, r, 3, now)
    collect(con, logs.root)
    assert accuracy.record_live(con, now) == 1
    assert accuracy.record_live(con, now + 1000) == 0       # same call count: not again
    assert SECRET not in str(con.execute("SELECT * FROM forecast_snaps").fetchall())
    cases, other = accuracy.live_cases(con, now)
    assert cases == [] and other == dict(pending=1, unresolved=0)   # still running: not scored yet

    _live(logs, r, 6, now, finished=True)                   # it ended after 6 calls
    collect(con, logs.root)
    cases, other = accuracy.live_cases(con, now + 5000)
    assert len(cases) == 1 and other["pending"] == 0
    k = cases[0]
    assert (k["calls"], k["calls_p50"], k["actual"]) == (3, 8, 6)
    d = accuracy.build(con, now + 5000)
    o = d["live"]["overall"]
    assert o["n"] == 1 and o["cov_p50"] == 1.0 and o["cov_p85"] == 1.0 and o["verdict"] == "few"
    assert d["live"]["sessions"][0]["runs"][0]["points"] == [[3, 8, 8]]
    assert d["backtest"]["overall"]["n"] > 0                 # the replay works on the same history


def test_closed_sessions_are_unresolved_not_misses(con):
    t = 1_790_000_000_000
    con.execute("INSERT INTO forecast_snaps VALUES ('s','a','p','Explore',?,3,1000,8,12,60000,90000,12,'all')", (t,))
    con.execute("INSERT INTO calls (msg_id, ts, ts_ms, session_id, project_dir, agent_id, is_sidechain, input, output, "
                "cache_read, cache_5m, cache_1h, thinking, web_search_n, web_fetch_n, prev_result_bytes, "
                "prev_result_image, prev_attach_bytes) VALUES ('m','x',?,'s','p','a',1,0,0,0,0,0,0,0,0,0,0,0)", (t,))
    cases, other = accuracy.live_cases(con, t + 2 * 3_600_000)
    assert cases == [] and other == dict(pending=0, unresolved=1)


def test_calibration_verdict():
    assert accuracy.calibration(5, .5) == "few"
    assert accuracy.calibration(100, .95) == "wide" and accuracy.calibration(100, .85) == "ok"
    assert accuracy.calibration(100, .6) == "narrow"


def test_accuracy_page_is_self_contained(con):
    html = accpage.render(dict(accuracy.build(con, 1_790_000_000_000), header={}))
    assert "<script src" not in html and "https://" not in html.replace("https://github.com/leeoniya/uPlot", "")
    assert 'aria-current=page>예측 정확도' in html
