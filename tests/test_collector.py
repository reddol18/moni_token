import hashlib

from moni_token.collector import collect
from conftest import SECRET

T = "2026-10-06T03:31:{:02d}.000Z"


def calls(con):
    return {r[0]: r for r in con.execute("SELECT msg_id, output, session_id, trigger, prev_result_bytes, "
                                         "prev_result_image, is_sidechain, agent_id, input, cache_read FROM calls")}


def tools(con, mid):
    return sorted(n for (n,) in con.execute("SELECT name FROM call_tools WHERE msg_id=?", (mid,)))


def test_split_response_counted_once_with_max_output_and_all_tools(logs, con):
    p = logs.path()
    logs.write(p, logs.human(T.format(0)),
               logs.assistant("m1", T.format(1), out=5),
               logs.assistant("m1", T.format(1), out=300, tools=["Read"]),
               logs.assistant("m1", T.format(2), out=120, tools=["Edit"]))
    collect(con, logs.root)
    c = calls(con)
    assert list(c) == ["m1"]
    assert c["m1"][1] == 300 and c["m1"][8] == 2 and c["m1"][9] == 1000
    assert tools(con, "m1") == ["Edit", "Read"]


def test_cross_file_duplicate_counted_once(logs, con):
    a, b = logs.path(session="s1"), logs.path(session="s2")
    logs.write(a, logs.assistant("m1", T.format(1), session="s1", out=50))
    logs.write(b, logs.assistant("m1", T.format(1), session="s2", out=50, tools=["Read"]),
               logs.assistant("m2", T.format(3), session="s2"))
    collect(con, logs.root)
    c = calls(con)
    assert set(c) == {"m1", "m2"}
    assert len(tools(con, "m1")) == 1


def test_trigger_and_previous_tool_result_size(logs, con):
    p = logs.path()
    payload = "x" * 5000
    logs.write(p, logs.human(T.format(0)),
               logs.assistant("m1", T.format(1), tools=["Read"]),
               logs.assistant("m1", T.format(1)),           # continuation must not consume state
               logs.tool_result(T.format(2), payload, image=True),
               logs.assistant("m2", T.format(3)),
               logs.assistant("m3", T.format(4)))
    collect(con, logs.root)
    c = calls(con)
    assert c["m1"][3] == "human" and c["m1"][4] == 0
    assert c["m2"][3] == "tool_result" and c["m2"][4] > 5000 and c["m2"][5] == 1
    assert c["m3"][3] == "other" and c["m3"][4] == 0


def test_incremental_offsets_and_partial_line(logs, con):
    p = logs.path()
    logs.write(p, logs.assistant("m1", T.format(1)))
    s1 = collect(con, logs.root)
    assert s1.call_lines == 1
    # second run with no change reads nothing
    assert collect(con, logs.root).files_read == 0
    # half-written line is left for the next run
    full = (__import__("json").dumps(logs.assistant("m2", T.format(2))) + "\n").encode()
    with open(p, "ab") as f:
        f.write(full[:40])
    collect(con, logs.root)
    assert set(calls(con)) == {"m1"}
    with open(p, "ab") as f:
        f.write(full[40:])
    s3 = collect(con, logs.root)
    assert set(calls(con)) == {"m1", "m2"} and s3.call_lines == 1


def test_state_carries_across_runs(logs, con):
    p = logs.path()
    logs.write(p, logs.assistant("m1", T.format(1), tools=["Read"]), logs.tool_result(T.format(2), "y" * 800))
    collect(con, logs.root)
    logs.write(p, logs.assistant("m2", T.format(3)))
    collect(con, logs.root)
    c = calls(con)
    assert c["m2"][3] == "tool_result" and c["m2"][4] >= 800


def test_truncated_file_is_reread(logs, con):
    p = logs.path()
    logs.write(p, logs.assistant("m1", T.format(1)), logs.assistant("m2", T.format(2)))
    collect(con, logs.root)
    logs.write(p, logs.assistant("m3", T.format(3)), mode="w")
    collect(con, logs.root)
    assert set(calls(con)) == {"m1", "m2", "m3"}


def test_subagent_workflow_and_journal(logs, con):
    logs.write(logs.path(session="s1", agent="a1"),
               logs.assistant("m1", T.format(1), sidechain=True, agent="a1"))
    logs.write(logs.path(session="s1", agent="a2", workflow="wf_1"),
               logs.assistant("m2", T.format(2), sidechain=True, agent="a2"))
    j = logs.path(session="s1", agent="a2", workflow="wf_1").parent / "journal.jsonl"
    logs.write(j, logs.assistant("m3", T.format(3)))  # would be counted if journals weren't skipped
    collect(con, logs.root)
    c = calls(con)
    assert set(c) == {"m1", "m2"}
    assert c["m1"][6] == 1 and c["m1"][7] == "a1"


def test_synthetic_messages_ignored(logs, con):
    logs.write(logs.path(), logs.assistant("m1", T.format(1), model="<synthetic>"))
    collect(con, logs.root)
    assert calls(con) == {}


def test_no_message_text_reaches_database(logs, con, tmp_path):
    p = logs.path()
    logs.write(p, logs.human(T.format(0)), logs.assistant("m1", T.format(1), tools=["Bash"]),
               logs.tool_result(T.format(2), SECRET))
    collect(con, logs.root)
    con.close()
    blob = b"".join(f.read_bytes() for f in tmp_path.glob("usage.db*"))
    assert SECRET.encode() not in blob


def test_logs_are_not_modified(logs, con):
    p = logs.path()
    logs.write(p, logs.human(T.format(0)), logs.assistant("m1", T.format(1)))
    before = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    collect(con, logs.root)
    assert (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) == before
