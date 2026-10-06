"""ADR-0003: limit-% view — measured (status line) and estimated (logs / calibration) series, % spikes, agent shares."""
import pytest

from moni_token.alerts import render
from moni_token.analysis import analyze
from moni_token.calibrate import add_manual
from moni_token.collector import collect
from moni_token.pctseries import pct_at, series
from moni_token.settings import SpikeParams
from moni_token.spikes import detect

from test_m2 import iso

H, M = 3_600_000, 60_000
T0 = 1_791_244_800_000
SONNET = dict(model="claude-sonnet-5", cr=0, out=0)


def obs(con, ts, pct, reset, kind="five_hour"):
    con.execute("INSERT INTO limit_obs VALUES (?,?,?,?,?,?)", (ts, "statusline", kind, reset, "", pct))


def test_measured_series_steps_and_resets_to_zero(con):
    reset = T0 + 2 * H
    obs(con, T0 + 10 * M, 20.0, reset)
    obs(con, T0 + 60 * M, 35.0, reset)
    s = series(con, "five_hour", T0, T0 + 3 * H)
    at = lambda t: pct_at(s, t)
    assert at(T0 + 30 * M) == (20.0, "measured")
    assert at(T0 + 90 * M) == (35.0, "measured")
    assert at(T0 + 2 * H + 5 * M) == (0.0, "measured")     # window reset


def test_estimated_series_from_manual_calibration(logs, con):
    # $2 per call (1M input at sonnet $2); 10 calls inside one 5h window
    logs.write(logs.path(), *[logs.assistant(f"m{i}", iso(T0 + i * 10 * M + M), inp=1_000_000, **SONNET)
                              for i in range(10)])
    collect(con, logs.root)
    add_manual(con, T0, T0 + 50 * M, 5.0, T0)                # 5 calls = $10 -> 5%  => $2 per 1%
    assert pct_at(series(con, "five_hour", T0, T0 + 3 * H), T0 + 2 * H) == (None, "none")   # 1 sample: hidden
    add_manual(con, T0, T0 + 30 * M, 3.0, T0)
    add_manual(con, T0, T0 + 70 * M, 7.0, T0)
    s = series(con, "five_hour", T0, T0 + 3 * H)
    v, basis = pct_at(s, T0 + 2 * H)
    assert basis == "estimated" and v == pytest.approx(10.0)   # $20 / $2 per %
    assert s["n_samples"] == 3 and s["reliable"]


def test_pct_spike_detected_with_basis_and_agents(logs, con):
    logs.write(logs.path(project="E--a", session="a"),
               *[logs.assistant(f"a{i}", iso(T0 + 2 * H + i * M), session="a", inp=1_000_000, **SONNET) for i in range(6)])
    logs.write(logs.path(project="E--b", session="b"),
               *[logs.assistant(f"b{i}", iso(T0 + 2 * H + i * M + 30_000), session="b", inp=500_000, **SONNET)
                 for i in range(6)])
    logs.write(logs.path(project="E--a", session="a"), logs.assistant("a0x", iso(T0 + 10 * M), session="a",
                                                                      inp=1_000_000, **SONNET))
    collect(con, logs.root)
    for k in range(3):
        add_manual(con, T0 + k, T0 + H, 1.0, T0)              # $2 -> 1%
    hits = [h for h in detect(con, T0 + H, T0 + 3 * H, SpikeParams()) if h.kind == "pct"]
    assert len(hits) == 1
    e = hits[0].evidence
    assert e["basis"] == "estimated" and e["rise_pp"] >= 3.0
    a = analyze(con, hits[0].start_ms, hits[0].end_ms)
    shares = {g["project"]: g["share"] for g in a.agents}
    assert shares["E--a"] == pytest.approx(2 / 3, abs=0.01) and shares["E--b"] == pytest.approx(1 / 3, abs=0.01)
    title, body = render(hits[0], a)
    assert "%p" in title and "E--a 67%" in body and "$" not in title + body


def test_measured_rise_is_preferred(con):
    reset = T0 + 5 * H
    obs(con, T0, 10.0, reset)
    obs(con, T0 + 20 * M, 16.0, reset)
    hits = [h for h in detect(con, T0, T0 + H, SpikeParams()) if h.kind == "pct"]
    assert hits and hits[0].evidence["basis"] == "measured" and hits[0].evidence["to_pct"] == 16.0


def test_agent_shares_split_subagent_and_headless(logs, con):
    logs.write(logs.path(project="E--a", session="s"), logs.assistant("m1", iso(T0), session="s", inp=1_000_000, **SONNET))
    logs.write(logs.path(project="E--a", session="s", agent="x"),
               logs.assistant("m2", iso(T0 + M), session="s", sidechain=True, agent="x", inp=1_000_000, **SONNET))
    collect(con, logs.root)
    g = analyze(con, T0, T0 + H).agents[0]
    assert g["share"] == 1.0 and g["subagent_share"] == pytest.approx(0.5)
