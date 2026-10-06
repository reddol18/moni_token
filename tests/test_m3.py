import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from moni_token.analysis import CauseParams, analyze
from moni_token.collector import collect
from moni_token.events import list_events, record, record_spike
from moni_token.settings import SpikeParams
from moni_token.spikes import detect

H, M = 3_600_000, 60_000
T0 = 1_791_244_800_000  # 2026-10-06T00:00:00Z


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def codes(a):
    return [c.code for c in a.causes]


def test_long_context_many_calls(logs, con):
    p = logs.path(session="s1")
    logs.write(p, *[logs.assistant(f"m{i}", iso(T0 + i * M), session="s1", cr=400_000 + i * 1000, c1=1000)
                    for i in range(25)])
    collect(con, logs.root)
    a = analyze(con, T0, T0 + H)
    c = a.causes[0]
    assert c.code == "long_context" and c.facts["calls"] == 25 and c.facts["ctx_median"] >= 400_000
    assert c.facts["cache_read_share"] > 0.8


def test_idle_resume_names_gap_and_rewrite(logs, con):
    p = logs.path(session="s1")
    logs.write(p, logs.assistant("m0", iso(T0), session="s1", cr=450_000),
               logs.assistant("m1", iso(T0 + 3 * H), session="s1", cr=28_000, c1=458_116),
               *[logs.assistant(f"m{i}", iso(T0 + 3 * H + i * M), session="s1", cr=486_000) for i in range(2, 10)])
    collect(con, logs.root)
    a = analyze(con, T0 + 2 * H, T0 + 4 * H)
    c = a.causes[0]
    assert c.code == "idle_resume" and c.facts["idle_min"] == 180.0 and c.facts["cache_write_tokens"] == 458_116
    assert "1시간 넘게 쉰 큰 세션은 이어쓰지 말고 새 세션" in c.advice
    assert "1시간" in c.interpretation


def test_cache_miss_without_idle(logs, con):
    p = logs.path(session="s1")
    logs.write(p, *[logs.assistant(f"m{i}", iso(T0 + i * M), session="s1", cr=1000, c5=300_000) for i in range(4)])
    collect(con, logs.root)
    assert codes(analyze(con, T0 - 1, T0 + H))[0] == "cache_miss"


def test_big_input_then_rewrite(logs, con):
    p = logs.path(session="s1")
    logs.write(p, logs.assistant("m1", iso(T0), session="s1", tools=["Read"]),
               logs.tool_result(iso(T0 + M), "z" * 300_000, session="s1", image=True),
               logs.assistant("m2", iso(T0 + 2 * M), session="s1", c5=150_000, cr=10_000))
    collect(con, logs.root)
    a = analyze(con, T0, T0 + H)
    c = next(c for c in a.causes if c.code == "big_input")
    assert c.facts["max_bytes"] > 300_000 and c.facts["images"] == 1


def test_parallel_sessions(logs, con):
    for s in ("s1", "s2", "s3"):
        logs.write(logs.path(project=f"E--{s}", session=s),
                   *[logs.assistant(f"{s}m{i}", iso(T0 + i * M), session=s) for i in range(3)])
    collect(con, logs.root)
    c = next(c for c in analyze(con, T0, T0 + H).causes if c.code == "parallel")
    assert c.facts["active_sessions"] == 3


def test_subagents_and_headless_count_as_parallel(logs, con):
    logs.write(logs.path(session="s1"), logs.assistant("h1", iso(T0), session="s1", entrypoint="sdk-cli"))
    collect(con, logs.root)
    assert "parallel" in codes(analyze(con, T0, T0 + H))


def test_web_research_and_output_burst(logs, con):
    p = logs.path(session="s1")
    logs.write(p, *[logs.assistant(f"m{i}", iso(T0 + i * M), session="s1", cr=100, out=20_000,
                                   tools=["WebSearch", "WebFetch"]) for i in range(5)])
    collect(con, logs.root)
    cs = {c.code: c for c in analyze(con, T0, T0 + H).causes}
    assert cs["web_research"].facts["web_tool_calls"] == 10
    assert "과소" in cs["output_burst"].facts["note"]


def test_facts_and_interpretation_are_separate_and_filled(logs, con):
    p = logs.path(session="s1")
    logs.write(p, logs.assistant("m0", iso(T0), session="s1"),
               logs.assistant("m1", iso(T0 + 2 * H), session="s1", c1=300_000))
    collect(con, logs.root)
    for c in analyze(con, T0 + H, T0 + 3 * H).causes:
        assert c.interpretation.endswith("(추정)") and "(추정)" not in c.fact_text
        assert not re.search(r"\{\w+", c.fact_text + c.interpretation + c.advice)


def test_events_recorded_with_causes_and_spike(logs, con):
    p = logs.path(session="s1")
    logs.write(p, logs.assistant("m0", iso(T0), session="s1"),
               logs.assistant("m1", iso(T0 + 3 * H), session="s1", c1=458_116))
    collect(con, logs.root)
    hits = detect(con, T0, T0 + 4 * H, SpikeParams(floor_usd=100))
    eid, _ = record_spike(con, hits[0], T0 + 4 * H)
    record_spike(con, hits[0], T0 + 4 * H)          # idempotent
    ev = list_events(con)
    assert len(ev) == 1 and ev[0]["id"] == eid and ev[0]["causes"][0]["code"] == "idle_resume"
    assert "output" in ev[0]["note"]


# ---------- M3 acceptance on the real logs (2026-10-06 KST) ----------

REAL_DB = Path.home() / ".moni_token" / "usage.db"
KST = lambda h, m, day=6: int(datetime(2026, 10, day, h, m, tzinfo=timezone.utc).timestamp() * 1000) - 9 * H


@pytest.fixture
def real():
    import sqlite3
    if not REAL_DB.exists():
        pytest.skip("no local usage.db")
    con = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    if not con.execute("SELECT 1 FROM calls WHERE session_id LIKE 'bec0d341%' LIMIT 1").fetchone():
        pytest.skip("acceptance session not in local logs")
    yield con
    con.close()


def test_acceptance_a_long_context_many_calls(real):
    a = analyze(real, KST(12, 31), KST(12, 57))
    c = next(c for c in a.causes if c.scope.startswith("bec0d341"))
    assert c.code == "long_context"
    assert c.facts["calls"] >= 45 and c.facts["ctx_median"] >= 400_000 and c.facts["cache_read_share"] >= 0.8


def test_acceptance_b_idle_resume_1556(real):
    a = analyze(real, KST(15, 6), KST(16, 6))
    c = a.causes[0]
    assert c.code == "idle_resume" and c.scope.startswith("bec0d341")
    assert c.facts["cache_write_tokens"] == 458_116 and c.facts["idle_min"] >= 170 and c.facts["ttl"] == "1h"


def test_acceptance_c_parallel_sessions(real):
    a = analyze(real, KST(23, 45), KST(0, 0, day=7))
    c = next(c for c in a.causes if c.code == "parallel")
    assert c.facts["active_sessions"] >= 3
    assert {"moni_pod", "audio-agent", "fund_agent"} <= set(c.facts["projects"])
