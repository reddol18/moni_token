"""Issue #18 (ADR-0008): AWS cost in the status line at zero cost. No real AWS call is made here: a fake `aws`
(a Python script, or an in-process runner) answers instead."""
import ast
import json
import re
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from moni_token import cloud, mapserver, statusline
from moni_token.settings import (CloudParams, load_cloud_params, save_cloud_params, update_cloud_params,
                                 validate_cloud_fields)

ACCT = "123456789012"
SRC = Path(__file__).resolve().parents[1] / "src" / "moni_token"
H, M = 3_600_000, 60_000
NOW = 1_795_600_000_000   # 2026-11-25 (UTC)


def iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def answers(now=NOW, metric=0.88, actual="0.883", forecast="1.08"):
    return {
        "sts": {"Account": ACCT, "Arn": f"arn:aws:iam::{ACCT}:user/billing", "UserId": "AIDAEXAMPLE"},
        "cloudwatch": {"Label": "EstimatedCharges", "Datapoints": [
            {"Timestamp": iso(now - 6 * H), "Maximum": 0.5, "Unit": "None"},
            {"Timestamp": iso(now - 2 * H), "Maximum": metric, "Unit": "None"}]},
        "budgets": {"Budget": {"BudgetName": "b", "BudgetLimit": {"Amount": "1.0", "Unit": "USD"},
                               "CalculatedSpend": {"ActualSpend": {"Amount": actual, "Unit": "USD"},
                                                   **({"ForecastedSpend": {"Amount": forecast, "Unit": "USD"}}
                                                      if forecast else {})},
                               "LastUpdatedTime": iso(now - 3 * H)}}}


class FakeRun:
    """In-process stand-in for subprocess.run: records argv, answers like the AWS CLI."""

    def __init__(self, ans=None, fail=None):
        self.calls, self.ans, self.fail = [], ans or answers(), fail or {}

    def __call__(self, cmd, **kw):
        assert kw.get("timeout") and kw.get("capture_output") and "shell" not in kw
        self.calls.append(cmd)
        svc = cmd[1]
        if svc in self.fail:
            return subprocess.CompletedProcess(cmd, 254, b"", self.fail[svc].encode())
        return subprocess.CompletedProcess(cmd, 0, json.dumps(self.ans[svc]).encode(), b"")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONI_TOKEN_HOME", str(tmp_path))
    cloud._ACCOUNT.clear()
    return tmp_path


def on(**kw):
    return CloudParams(**{**dict(enabled=True, aws_profile="moni-token-billing", budget_name="monthly-1usd"), **kw})


# --- the allowlist ----------------------------------------------------------------------------------------------

def test_guard_allows_only_the_three_free_reads():
    assert cloud.guard(["cloudwatch", "get-metric-statistics", "--x"]) == ("cloudwatch", "get-metric-statistics")
    assert cloud.guard(["budgets", "describe-budget"]) and cloud.guard(["sts", "get-caller-identity"])
    for bad in (["ce", "get-cost-and-usage"], ["CE", "get-cost-forecast"], ["cloudwatch", "get-metric-data"],
                ["cloudwatch", "get-metric-statistics", "get-metric-data"], ["budgets", "create-budget"],
                ["ec2", "describe-instances"], ["sts"], [], ["cloudwatch", 1]):
        with pytest.raises(cloud.NotAllowed):
            cloud.guard(bad)


def test_a_refused_call_never_starts_a_process():
    run = FakeRun()
    aws = cloud.Aws(["aws"], "p", run=run)
    for bad in (["ce", "get-cost-and-usage"], ["cloudwatch", "get-metric-data"]):
        with pytest.raises(cloud.NotAllowed):
            aws.call(bad)
    assert run.calls == []


def test_every_aws_call_site_in_the_code_is_on_the_allowlist():
    """Fails if any code path could invoke `aws ce`, get-metric-data or anything else."""
    tree = ast.parse((SRC / "cloud.py").read_text(encoding="utf-8"))
    sites = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "call"]
    assert len(sites) >= 3
    for n in sites:
        lst = n.args[0]
        assert isinstance(lst, ast.List), ast.dump(n)
        head = tuple(e.value for e in lst.elts[:2])
        assert head in cloud.ALLOWED, head
        assert not any(isinstance(e, ast.Constant) and e.value == "get-metric-data" for e in lst.elts)
    runs = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in ("run", "Popen", "call")
            and isinstance(n.value, ast.Name) and n.value.id == "subprocess"]
    assert len(runs) == 1   # only _default_run, reached only from Aws.call after guard()


# --- parsing, check, privacy ------------------------------------------------------------------------------------

FAKE_AWS = r'''
import json, os, sys
d = os.environ["FAKE_AWS_DIR"]
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
sc = json.load(open(os.path.join(d, "scenario.json")))
svc = sys.argv[1]
if svc in sc.get("fail", {}):
    sys.stderr.write(sc["fail"][svc]); sys.exit(254)
print(json.dumps(sc["answers"][svc]))
'''


@pytest.fixture
def fake_aws(tmp_path, monkeypatch):
    d = tmp_path / "fakeaws"
    d.mkdir()
    (d / "aws_fake.py").write_text(FAKE_AWS, encoding="utf-8")
    monkeypatch.setenv("FAKE_AWS_DIR", str(d))

    def scenario(**kw):
        (d / "scenario.json").write_text(json.dumps(dict(answers=answers(int(time.time() * 1000)), **kw)),
                                         encoding="utf-8")
    scenario()
    calls = lambda: [json.loads(x) for x in (d / "calls.jsonl").read_text().splitlines()] \
        if (d / "calls.jsonl").exists() else []
    return [sys.executable, str(d / "aws_fake.py")], scenario, calls


def test_check_with_a_fake_aws_executable(home, fake_aws):
    exe, scenario, calls = fake_aws
    now = int(time.time() * 1000)
    r = cloud.check(on(), now, exe=exe)
    assert r["ok"] and [s["id"] for s in r["steps"]] == ["aws", "profile", "metric", "budget", "calls"]
    st = {s["id"]: s for s in r["steps"]}
    assert st["metric"]["value"]["amount"] == 0.88 and abs(st["metric"]["value"]["ts_ms"] - (now - 2 * H)) < 5000
    assert st["budget"]["value"]["actual"] == 0.883 and st["budget"]["value"]["forecast"] == 1.08
    assert st["calls"]["value"]["metric"] == 4320 and st["calls"]["value"]["budget"] == 120
    got = calls()
    assert [c[:2] for c in got] == [["sts", "get-caller-identity"], ["cloudwatch", "get-metric-statistics"],
                                    ["budgets", "describe-budget"]]
    assert all("--profile" in c and "moni-token-billing" in c and "us-east-1" in c for c in got)
    cw = got[1]
    assert cw[cw.index("--namespace") + 1] == "AWS/Billing" and "Name=Currency,Value=USD" in cw
    # the account ID went to the CLI as an argument, but is in no result and no file
    text = (home / "cloud_cost.json").read_text()
    assert ACCT not in text and ACCT not in json.dumps(r) and "arn:" not in text
    assert "monthly-1usd" not in text and "moni-token-billing" not in text   # no resource names either
    s = json.loads(text)
    assert s["calls"] == dict(cloudwatch=1, budgets=1, sts=1) and s["check"]["ok"]
    assert cloud.check_passed(on(), now) and not cloud.check_passed(on(budget_name="other"), now)


def test_check_reports_each_failure_with_a_code_only(home, fake_aws):
    exe, scenario, calls = fake_aws
    scenario(fail=dict(cloudwatch=f"An error occurred (AccessDenied) when calling the GetMetricStatistics operation: "
                                  f"User: arn:aws:iam::{ACCT}:user/x is not authorized"))
    r = cloud.check(on(budget_name=""), int(time.time() * 1000), exe=exe)
    st = {s["id"]: s for s in r["steps"]}
    assert not r["ok"] and st["metric"] == dict(id="metric", ok=False, code="AccessDenied")
    assert st["budget"]["code"] == "NoBudget" and ACCT not in json.dumps(r)
    assert ACCT not in (home / "cloud_cost.json").read_text()
    scenario(fail=dict(sts="\nThe config profile (nope) could not be found\n"))
    r = cloud.check(on(), int(time.time() * 1000), exe=exe)
    assert [s["code"] for s in r["steps"][1:4]] == ["ProfileNotFound", "Skipped", "Skipped"]


def test_check_without_aws_cli(home, monkeypatch):
    monkeypatch.setattr(cloud, "resolve_aws", lambda p="": None)
    run = FakeRun()
    r = cloud.check(on(), NOW, run=run)
    assert not r["ok"] and r["steps"][0]["code"] == "AwsNotFound" and run.calls == []


def test_error_codes_never_carry_the_message():
    assert cloud.error_code(b"An error occurred (AccessDeniedException) when calling DescribeBudget: "
                            b"arn:aws:iam::123456789012:user/x") == "AccessDenied"
    assert cloud.error_code("An error occurred (NotFoundException) when ...") == "NotFound"
    assert cloud.error_code("Unable to locate credentials. You can configure ...") == "NoCredentials"
    assert cloud.error_code("Could not connect to the endpoint URL") == "NoNetwork"
    assert cloud.error_code("something 123456789012") == "Error"


def test_metric_without_datapoints_is_no_data():
    a = answers()
    a["cloudwatch"]["Datapoints"] = []
    with pytest.raises(cloud.AwsError) as e:
        cloud.fetch_metric(cloud.Aws(["aws"], "", run=FakeRun(a)), NOW)
    assert e.value.code == "NoData"


def test_resolve_aws_prefers_config_then_path_then_folders(tmp_path, monkeypatch):
    exe = tmp_path / "aws.exe"
    exe.write_bytes(b"")
    assert cloud.resolve_aws(str(exe)) == str(exe)
    assert cloud.resolve_aws(str(tmp_path / "missing" / "aws.exe")) is None
    (tmp_path / "evil.exe").write_bytes(b"")
    assert cloud.resolve_aws(str(tmp_path / "evil.exe")) is None
    monkeypatch.setattr(cloud.shutil, "which", lambda n: None)
    monkeypatch.setattr(cloud, "_candidates", lambda: [tmp_path / "nope.exe", exe])
    assert cloud.resolve_aws("") == str(exe)


# --- the refresher ------------------------------------------------------------------------------------------------

def test_disabled_means_zero_subprocesses(home, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a process was started while the feature is off")
    for name in ("run", "Popen", "call", "check_output"):
        monkeypatch.setattr(subprocess, name, boom)
    assert load_cloud_params().enabled is False                         # off by default: no config at all
    assert cloud.tick(NOW) is None
    (home / "config.toml").write_text('[cloud]\nenabled = false\naws_profile = "x"\n', encoding="utf-8")
    assert cloud.tick(NOW) is None and not (home / "cloud_cost.json").exists()
    stop = threading.Event()
    t = threading.Thread(target=cloud.run_forever, args=(stop, 0.01))
    t.start()
    time.sleep(0.1)
    stop.set()
    t.join(2)
    assert not (home / "cloud_cost.json").exists()
    assert cloud.view(load_cloud_params(), NOW, served=True, token="t")["enabled"] is False
    assert statusline.aws_segment(NOW, "en") == ""


def test_refresher_intervals(home):
    run = FakeRun(answers(NOW))
    st = cloud.tick(NOW, on(), exe=["aws"], run=run)
    assert [c[1] for c in run.calls] == ["cloudwatch", "sts", "budgets"]
    assert st["metric"]["amount"] == 0.88 and st["budget"]["forecast"] == 1.08
    cloud.tick(NOW + 5 * M, on(), exe=["aws"], run=run)
    assert len(run.calls) == 3                                           # nothing due yet
    cloud.tick(NOW + 10 * M, on(), exe=["aws"], run=run)
    assert [c[1] for c in run.calls[3:]] == ["cloudwatch"]               # metric every 10 min; sts kept in memory
    cloud.tick(NOW + 6 * H, on(), exe=["aws"], run=run)
    assert [c[1] for c in run.calls[4:]] == ["cloudwatch", "budgets"]    # budget every 6 h
    s = json.loads((home / "cloud_cost.json").read_text())
    assert s["calls"] == dict(cloudwatch=3, budgets=2, sts=1) and ACCT not in json.dumps(s)


def test_failures_back_off(home):
    run = FakeRun(fail=dict(cloudwatch="An error occurred (Throttling) when calling"))
    p = on(budget_name="")
    t = NOW
    gaps = []
    for _ in range(4):
        st = cloud.tick(t, p, exe=["aws"], run=run)
        gaps.append((st["next"]["metric"] - t) // M)
        t = st["next"]["metric"]
    assert gaps == [20, 40, 80, 160] and st["err"]["metric"]["code"] == "Throttling"
    run.fail = {}
    st = cloud.tick(t, p, exe=["aws"], run=run)
    assert st["err"]["metric"] is None and st["metric"]["amount"] == 0.88 and (st["next"]["metric"] - t) // M == 10


def test_monthly_cap_stops_calls_until_next_month(home):
    run = FakeRun()
    p = on(budget_name="", max_calls_month=2)
    cloud.tick(NOW, p, exe=["aws"], run=run)
    cloud.tick(NOW + 10 * M, p, exe=["aws"], run=run)
    st = cloud.tick(NOW + 20 * M, p, exe=["aws"], run=run)
    assert len(run.calls) == 2 and st["err"]["metric"]["code"] == "CallCap"
    assert st["next"]["metric"] == cloud.next_month_ms(NOW)
    st = cloud.tick(cloud.next_month_ms(NOW) + 1, p, exe=["aws"], run=run)   # a new month resets the counter
    assert len(run.calls) == 3 and st["calls"]["cloudwatch"] == 1


def test_expected_calls_per_month():
    assert cloud.expected_calls(on()) == dict(metric=4320, budget=120, total=4440)
    assert cloud.expected_calls(on(budget_name="")) == dict(metric=4320, budget=0, total=4320)


# --- the status line ----------------------------------------------------------------------------------------------

def _enable(home):
    (home / "config.toml").write_text('lang = "en"\n[cloud]\nenabled = true\n', encoding="utf-8")


def test_status_line_segment_en_ko_and_stale(home):
    _enable(home)
    now = int(time.time() * 1000)
    cloud.tick(now, on(), exe=["aws"], run=FakeRun(answers(now)))
    assert statusline.aws_segment(now, "en") == "AWS $0.88 (fc $1.08) · 2h"
    assert statusline.aws_segment(now, "ko") == "AWS $0.88 (예상 $1.08) · 2시간"
    assert statusline.aws_segment(now + 13 * H, "en") == ""               # stale: hidden
    (home / "config.toml").write_text('[cloud]\nenabled = false\n', encoding="utf-8")
    assert statusline.aws_segment(now, "en") == ""                        # disabled: hidden


def test_status_line_without_forecast_and_unknown(home):
    _enable(home)
    now = int(time.time() * 1000)
    cloud.tick(now, on(), exe=["aws"], run=FakeRun(answers(now, forecast=None)))
    assert statusline.aws_segment(now + 30 * M, "en") == "AWS $0.88 · 2h"
    (home / "cloud_cost.json").unlink()
    cloud.tick(now, on(), exe=["aws"], run=FakeRun(fail=dict(sts="x", cloudwatch="x", budgets="x")))
    assert statusline.aws_segment(now, "en") == "AWS ?"


def test_status_line_main_puts_the_segment_in_front(home, monkeypatch, capsysbinary):
    _enable(home)
    now = int(time.time() * 1000)
    cloud.tick(now, on(), exe=["aws"], run=FakeRun(answers(now)))
    import io
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(b'{"rate_limits":{}}')})())
    assert statusline.main(["--lang", "en"]) == 0
    assert capsysbinary.readouterr().out.decode("utf-8").startswith("AWS $0.88 (fc $1.08) · 2h · ")


# --- config -------------------------------------------------------------------------------------------------------

def test_config_round_trip_keeps_the_rest_of_the_file(home):
    cfg = home / "config.toml"
    cfg.write_text('# my comment\nlang = "ko"\n\n[spike]\nfloor_usd = 12.14   # keep\n\n# map below\n[map]\nuia = true\n',
                   encoding="utf-8")
    p = update_cloud_params(dict(enabled=True, aws_profile="moni-token-billing", budget_name="pnu web (1usd)",
                                 aws_path=""))
    assert load_cloud_params() == p and p.metric_min == 10 and p.max_calls_month == 6000
    update_cloud_params(dict(enabled=False, aws_path=""))
    text = cfg.read_text(encoding="utf-8")
    assert text.count("[cloud]") == 1 and "# my comment" in text and "floor_usd = 12.14   # keep" in text
    assert "# map below\n[map]\nuia = true" in text
    q = load_cloud_params()
    assert not q.enabled and q.aws_profile == "moni-token-billing" and q.budget_name == "pnu web (1usd)"
    from moni_token.settings import load_map_params, load_spike_params
    assert load_spike_params().floor_usd == 12.14 and load_map_params().uia is True
    win = CloudParams(aws_path="F:\\aws_cli\\aws.exe")
    save_cloud_params(win)
    assert load_cloud_params().aws_path == "F:\\aws_cli\\aws.exe"


def test_bad_numbers_in_config_fall_back(home):
    (home / "config.toml").write_text('[cloud]\nenabled = "yes"\nmetric_min = 1\nbudget_h = "x"\n', encoding="utf-8")
    p = load_cloud_params()
    assert p.enabled is False and p.metric_min == 5 and p.budget_h == 6


def test_validate_fields(tmp_path):
    exe = tmp_path / "aws.exe"
    exe.write_bytes(b"")
    ok, bad = validate_cloud_fields(dict(enabled=True, aws_profile="moni-token-billing",
                                         budget_name="pnu-web-monthly-1usd", aws_path=str(exe)))
    assert not bad and ok["aws_path"] == str(exe)
    for d, field in ((dict(account_id="1"), "account_id"), (dict(aws_profile="a;rm -rf"), "aws_profile"),
                     (dict(aws_profile="x" * 65), "aws_profile"), (dict(budget_name="a:b"), "budget_name"),
                     (dict(aws_path=str(tmp_path / "x.exe")), "aws_path"),
                     (dict(aws_path=str(tmp_path / "no" / "aws.exe")), "aws_path"), (dict(aws_path="aws.exe"), "aws_path"),
                     (dict(enabled="true"), "enabled"), (dict(aws_profile=3), "aws_profile")):
        assert field in validate_cloud_fields(d)[1], d
    assert validate_cloud_fields([1])[1] == ["body"]


# --- the dashboard endpoint ---------------------------------------------------------------------------------------

@pytest.fixture
def server(home, logs, monkeypatch):
    run = FakeRun(answers(int(time.time() * 1000)))
    monkeypatch.setattr(cloud, "_default_run", run)
    monkeypatch.setattr(cloud, "resolve_aws", lambda p="": "C:\\fake\\aws.exe")
    srv = ThreadingHTTPServer((mapserver.HOST, 0), mapserver.make_handler(home / "m.db", logs.root, "en"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def req(method, path, body=None, headers=None, raw=None):
        c = HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Host": f"127.0.0.1:{port}"}
        h.update(headers or {})
        c.request(method, path, body=raw if raw is not None else (json.dumps(body) if body is not None else None),
                  headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        return r.status, data

    def post(path, body, token=None, origin=None, ctype="application/json", **kw):
        h = {"Content-Type": ctype, "Origin": origin or f"http://127.0.0.1:{port}"}
        if token is not False:
            h["X-Moni-Token"] = token or srv.RequestHandlerClass.token
        h.update(kw.get("headers") or {})
        s, d = req("POST", path, body, h, kw.get("raw"))
        return s, (json.loads(d) if d.startswith("{") else d)
    yield srv, req, post, run, port
    srv.shutdown()
    srv.server_close()


def test_usage_page_off_by_default_shows_the_guide_and_token(server):
    srv, req, post, run, port = server
    s, html = req("GET", "/usage?lang=en")
    assert s == 200 and srv.RequestHandlerClass.token in html and "function cloudForm" in html
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1))
    c = data["cloud"]
    assert c["served"] and c["enabled"] is False and "budgets:ViewBudget" in c["policy"]
    assert "winget install -e --id Amazon.AWSCLI" in html and "Receive CloudWatch billing alerts" in html
    assert run.calls == []                                                 # showing the page calls nothing
    s, ko = req("GET", "/usage?lang=ko")
    assert "1회 설정" in ko and "왜 비용이 0인가" in ko


def test_post_security(server):
    srv, req, post, run, port = server
    body = dict(aws_profile="moni-token-billing")
    assert post("/api/cloud/check", body, origin="http://evil.example")[0] == 403
    assert post("/api/cloud/check", body, origin=f"http://127.0.0.1:{port + 1}")[0] == 403
    assert post("/api/cloud/check", body, token=False)[0] == 403
    assert post("/api/cloud/check", body, token="wrong")[0] == 403
    assert post("/api/cloud/check", body, ctype="text/plain")[0] == 403
    assert post("/api/cloud/check", body, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    s, d = req("POST", "/api/cloud/check", body, {"Content-Type": "application/json", "Host": "evil.example",
                                                   "X-Moni-Token": srv.RequestHandlerClass.token,
                                                   "Origin": "http://evil.example"})
    assert s == 403                                                        # DNS rebinding
    s, d = post("/api/cloud/settings", dict(enabled=False, account_id=ACCT))
    assert s == 400 and d["fields"] == ["account_id"]
    s, d = post("/api/cloud/settings", dict(aws_profile="x y;z"))
    assert s == 400 and d["fields"] == ["aws_profile"]
    s, d = post("/api/cloud/settings", dict(aws_path="C:\\Windows\\System32\\cmd.exe"))
    assert s == 400 and d["fields"] == ["aws_path"]
    assert post("/api/cloud/check", dict(enabled=True))[0] == 400
    assert post("/api/cloud/check", None, raw="{not json")[0] == 400
    assert post("/api/cloud/check", None, raw="x" * 5000)[0] == 413
    assert post("/api/nope", body)[0] == 404
    assert run.calls == []                                                 # nothing refused reached the AWS CLI


def test_enable_needs_a_passed_check_then_saves_config(server, home):
    srv, req, post, run, port = server
    fields = dict(aws_profile="moni-token-billing", budget_name="pnu-web-monthly-1usd", aws_path="")
    s, d = post("/api/cloud/settings", dict(enabled=True, **fields))
    assert s == 409 and d["error"] == "check_first" and not load_cloud_params().enabled and run.calls == []
    s, d = post("/api/cloud/check", fields)
    assert s == 200 and d["check"]["ok"] and len(run.calls) == 3
    assert d["view"]["enabled"] is False and ACCT not in json.dumps(d)
    s, d = post("/api/cloud/settings", dict(enabled=True, **fields), origin=f"http://localhost:{port}",
                headers={"Host": f"localhost:{port}"})
    assert s == 200 and d["view"]["enabled"] and d["view"]["metric"]["amount"] == 0.88
    p = load_cloud_params()
    assert p.enabled and p.aws_profile == "moni-token-billing" and p.budget_name == "pnu-web-monthly-1usd"
    s, d = post("/api/cloud/settings", dict(budget_name="another"))     # changed settings need a new check
    assert s == 409 and load_cloud_params().budget_name == "pnu-web-monthly-1usd"
    s, d = post("/api/cloud/settings", dict(enabled=False))
    assert s == 200 and not load_cloud_params().enabled and len(run.calls) == 3


def test_static_report_has_no_form_and_no_token(home, con):
    from moni_token.report import build_data, render
    html = render(build_data(con, NOW, lang="en"), lang="en")
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert data["cloud"]["served"] is False and "token" not in data["cloud"] and "settings" not in data["cloud"]
    assert "This file has no server" in html and "X-Moni-Token" not in html and "fetch(" not in html


def test_cli_cloud_status_makes_no_call(home, monkeypatch, capsys):
    from moni_token.cli import main
    monkeypatch.setattr(cloud, "_default_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert main(["--lang", "en", "cloud", "status"]) == 0
    assert "AWS cost: OFF" in capsys.readouterr().out
