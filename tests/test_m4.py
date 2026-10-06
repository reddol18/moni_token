import json
import re
from datetime import datetime, timezone

from moni_token.collector import collect
from moni_token.events import record_spike
from moni_token.report import build_data, render, write_report
from moni_token.settings import SpikeParams
from moni_token.spikes import detect
from conftest import SECRET

H, M = 3_600_000, 60_000
T0 = 1_791_244_800_000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def seeded(logs, con):
    for i, proj in enumerate(["E--a", "E--b", "E--c", "E--d", "E--e", "E--f", "E--g", "E--h"]):
        logs.write(logs.path(project=proj, session=f"s{i}"),
                   *[logs.assistant(f"{proj}{j}", iso(T0 + j * 10 * M), session=f"s{i}", cr=10_000 * (i + 1))
                     for j in range(6)])
    logs.write(logs.path(project="E--a", session="s0"), logs.tool_result(iso(T0 + 3 * H - M), "</script><b>" + SECRET),
               logs.assistant("big", iso(T0 + 3 * H), session="s0", c1=400_000, tools=["Read"]))
    collect(con, logs.root)
    for s in detect(con, T0, T0 + 4 * H, SpikeParams(floor_usd=1000)):
        record_spike(con, s, T0 + 4 * H)


def test_report_data_has_limit_series_and_agent_shares(logs, con):
    seeded(logs, con)
    d = build_data(con, T0 + 4 * H, days=1)
    for k in ("five_hour", "seven_day"):
        s = d[k]
        assert len(s["m"]) == len(s["e"]) > 0
    assert d["five_hour"]["current"] is None          # no status line, no calibration -> no % at all
    ev = d["events"][0]
    assert ev["kind"] == "cache_write" and ev["calls"][0]["tools"] == ["Read"]
    assert abs(sum(g["share"] for g in ev["agents"]) - 1) < 0.01


def test_report_is_offline_and_has_no_log_text(logs, con, tmp_path):
    seeded(logs, con)
    out = write_report(con, tmp_path / "r.html", now_ms=T0 + 4 * H, days=1)
    html = out.read_text(encoding="utf-8")
    assert SECRET not in html
    assert not re.search(r"""(src|href)\s*=\s*["']?https?:""", html), "external resource"
    assert "fetch(" not in html and "XMLHttpRequest" not in html
    blob = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
    assert json.loads(blob)["events"]


def test_embedded_json_cannot_break_out_of_script():
    html = render({"x": "</script><script>alert(1)</script>", "events": []})
    assert html.count("</script>") == 2   # data block + main script, nothing injected
