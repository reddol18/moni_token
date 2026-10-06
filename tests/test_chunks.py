"""Raw chunks shipped to the API: measured size, carried until compaction, shares per agent."""
import json

import pytest

from moni_token.chunks import analyze_chunks, category
from moni_token.collector import collect

from test_m2 import iso

M = 60_000
T0 = 1_791_244_800_000


def compact_line(ts, session="s"):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": ts, "sessionId": session, "uuid": "c1"}


def test_chunk_size_is_measured_and_carried_until_compaction(logs, con):
    p = logs.path(session="s")
    m1 = logs.assistant("m1", iso(T0), session="s", cr=20_000, out=100, tools=["Read"])
    read_id = m1["message"]["content"][1]["id"]
    logs.write(p, m1,
               logs.tool_result(iso(T0 + M), "x" * 160_000, session="s", tool_use_id=read_id),
               # context grows by 40,100 = previous output 100 + the 40k-token file
               logs.assistant("m2", iso(T0 + 2 * M), session="s", cr=20_000, c1=40_100, out=50),
               logs.assistant("m3", iso(T0 + 3 * M), session="s", cr=60_150, out=50),
               logs.assistant("m4", iso(T0 + 4 * M), session="s", cr=60_200, out=50),
               compact_line(iso(T0 + 5 * M)),
               logs.assistant("m5", iso(T0 + 6 * M), session="s", cr=10_000, out=50))
    collect(con, logs.root)
    r = analyze_chunks(con, T0, T0 + 10 * M, min_tokens=5_000)
    g = r["agents"][0]
    assert g["chunks"] == 1 and g["median_chunk"] == 40_000
    assert g["carried_tokens"] == 40_000 * 3            # written at m2, re-read by m3 and m4, gone after compaction
    assert g["categories"][0]["category"] == "파일 읽기"
    assert 0 < g["token_share"] < 1 and 0 < g["cost_share"] < 1


def test_small_results_and_unknown_growth_are_not_chunks(logs, con):
    p = logs.path(session="s")
    logs.write(p, logs.assistant("m1", iso(T0), session="s", cr=20_000, out=10, tools=["Grep"]),
               logs.tool_result(iso(T0 + M), "y" * 4_000, session="s"),
               logs.assistant("m2", iso(T0 + 2 * M), session="s", cr=21_010, out=10))
    collect(con, logs.root)
    assert analyze_chunks(con, T0, T0 + 10 * M)["overall"]["chunks"] == 0


def test_categories():
    assert category("mcp__mnemento__query") == "MCP(DB·외부 도구)"
    assert category("PowerShell") == "셸 출력" and category("Skill") == "기타(Skill)"


def test_per_result_state_survives_incremental_runs(logs, con):
    p = logs.path(session="s")
    logs.write(p, logs.assistant("m1", iso(T0), session="s", cr=20_000, out=0, tools=["Read"]),
               logs.tool_result(iso(T0 + M), "x" * 80_000, session="s"))
    collect(con, logs.root)
    logs.write(p, logs.assistant("m2", iso(T0 + 2 * M), session="s", cr=20_000, c5=20_000, out=0))
    collect(con, logs.root)
    assert con.execute("SELECT msg_id, bytes FROM tool_results").fetchall()[0][0] == "m2"
