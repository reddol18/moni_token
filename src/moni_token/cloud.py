"""AWS account cost at zero cost (issue #18, ADR-0008). Opt-in: off by default, and while off nothing here starts a
process.

Only three read-only, free AWS CLI operations may ever run (`guard`):
  - `cloudwatch get-metric-statistics` on AWS/Billing EstimatedCharges (always-free 1M API requests a month;
    `get-metric-data` is always charged and refused),
  - `budgets describe-budget` (budgets without actions are free),
  - `sts get-caller-identity` (free; gives the account ID needed by describe-budget - kept in memory only).
Cost Explorer (`aws ce`, $0.01 per request) and anything else is refused in code.

A background thread in `moni_token map` (`run_forever`) refreshes `~/.moni_token/cloud_cost.json`; the status line
only reads that file. Stored: amounts, currency, timestamps, call counts, error codes. Never the account ID, keys,
ARNs or AWS's error messages (they can contain the account ID): only the error code is kept.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .settings import CloudParams, load_cloud_params

ALLOWED = {("cloudwatch", "get-metric-statistics"), ("budgets", "describe-budget"), ("sts", "get-caller-identity")}
BANNED_SERVICES = {"ce"}                        # Cost Explorer API: $0.01 per request, nothing free
BANNED_WORDS = {"get-metric-data"}              # CloudWatch GetMetricData: always charged
SERVICE_KEY = {"cloudwatch": "cloudwatch", "budgets": "budgets", "sts": "sts"}
REGION = "us-east-1"                            # billing metrics exist only there; Budgets is global
TIMEOUT_S = 30
MIN = 60_000
HOUR = 3_600_000
STALE_MS = 12 * HOUR                            # older than this: the status line hides the segment
LOCK = threading.Lock()
_ACCOUNT: dict[str, str] = {}                   # profile -> account ID, this process only (never written anywhere)

POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": ["cloudwatch:GetMetricStatistics", "aws-portal:ViewBilling"], "Resource": "*"},
        {"Effect": "Allow", "Action": "budgets:ViewBudget",
         "Resource": "arn:aws:budgets::ACCOUNT_ID:budget/BUDGET_NAME"}]}, indent=2)


class NotAllowed(Exception):
    """An AWS CLI call outside the allowlist was attempted. A bug, never a user error."""


class AwsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def guard(args: list[str]) -> tuple[str, str]:
    """The only gate to `aws`. Raises NotAllowed for anything but the three free read-only operations."""
    if len(args) < 2 or not all(isinstance(a, str) for a in args):
        raise NotAllowed("malformed")
    svc, op = args[0].lower(), args[1].lower()
    if svc in BANNED_SERVICES or any(a.lower() in BANNED_WORDS for a in args) or (svc, op) not in ALLOWED:
        raise NotAllowed(f"{svc} {op}")
    return svc, op


# --- finding the AWS CLI (no process is started) ---------------------------------------------------------------

def _candidates() -> list[Path]:
    if sys.platform == "win32":
        out = []
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            if os.environ.get(var):
                out.append(Path(os.environ[var]) / "Amazon" / "AWSCLIV2" / "aws.exe")
        if os.environ.get("LOCALAPPDATA"):
            out.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Amazon" / "AWSCLIV2" / "aws.exe")
        return out
    return [Path(p) for p in ("/usr/local/bin/aws", "/opt/homebrew/bin/aws", "/usr/bin/aws")]


def aws_name_ok(p: Path) -> bool:
    return p.name.lower() in ("aws.exe", "aws")


def resolve_aws(aws_path: str = "") -> str | None:
    """config `aws_path`, then PATH, then the usual install folders. A process started before the CLI was installed
    (the map task) does not see the new PATH, hence the folders."""
    if aws_path:
        p = Path(aws_path)
        return str(p) if p.is_file() and aws_name_ok(p) else None
    w = shutil.which("aws")
    if w and aws_name_ok(Path(w)):
        return w
    for c in _candidates():
        if c.is_file():
            return str(c)
    return None


# --- calling it ---------------------------------------------------------------------------------------------------

def error_code(stderr: bytes | str) -> str:
    """Only a code survives: AWS messages can contain the account ID or an ARN."""
    s = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else str(stderr or "")
    m = re.search(r"An error occurred \(([A-Za-z0-9_.]{1,64})\)", s)
    if m:
        code = m.group(1)
        if code in ("AccessDeniedException", "UnauthorizedOperation", "AccessDenied", "UnauthorizedAccess"):
            return "AccessDenied"
        if code in ("NotFoundException", "NotFound"):
            return "NotFound"
        return code
    if "could not be found" in s and "profile" in s:
        return "ProfileNotFound"
    if "Unable to locate credentials" in s or "NoCredentials" in s:
        return "NoCredentials"
    if "Could not connect to the endpoint" in s or "EndpointConnectionError" in s:
        return "NoNetwork"
    if "Token has expired" in s or "ExpiredToken" in s:
        return "ExpiredToken"
    return "Error"


def _default_run(cmd, **kw):
    return subprocess.run(cmd, **kw)


class Aws:
    """`exe` is the command prefix: [aws.exe] in use, [python, fake_aws.py] in tests. `count(service)` is called before
    each call and raises AwsError("CallCap") past the monthly cap."""

    def __init__(self, exe: list[str], profile: str, count=None, run=None):
        self.exe, self.profile, self.count, self.run = list(exe), profile, count, run or _default_run

    def call(self, args: list[str]):
        svc, _ = guard(args)
        if self.count:
            self.count(svc)
        cmd = [*self.exe, *args, "--region", REGION, "--output", "json", "--no-cli-pager"]
        if self.profile:
            cmd += ["--profile", self.profile]
        env = {**os.environ, "AWS_PAGER": "", "AWS_CLI_AUTO_PROMPT": "off"}
        try:
            r = self.run(cmd, capture_output=True, timeout=TIMEOUT_S, env=env, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise AwsError("Timeout") from None
        except OSError:
            raise AwsError("AwsNotFound") from None
        if r.returncode != 0:
            raise AwsError(error_code(r.stderr))
        try:
            return json.loads(r.stdout or b"{}")
        except ValueError:
            raise AwsError("BadOutput") from None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_ms(v) -> int | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v * 1000) if v < 1e11 else int(v)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            try:
                return _ts_ms(float(v))
            except ValueError:
                return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    return None


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) < 1e12 else None


def fetch_metric(aws: Aws, now_ms: int) -> dict:
    """Latest month-to-date EstimatedCharges (USD) of the whole account."""
    end = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    d = aws.call(["cloudwatch", "get-metric-statistics", "--namespace", "AWS/Billing",
                  "--metric-name", "EstimatedCharges", "--dimensions", "Name=Currency,Value=USD",
                  "--start-time", _iso(end - timedelta(days=2)), "--end-time", _iso(end),
                  "--period", "600", "--statistics", "Maximum"])
    pts = [(t, v) for p in (d.get("Datapoints") or []) if isinstance(p, dict)
           for t, v in [(_ts_ms(p.get("Timestamp")), _num(p.get("Maximum")))] if t and v is not None]
    if not pts:
        raise AwsError("NoData")   # billing alerts off, or an Organizations member account
    t, v = max(pts)
    return dict(amount=round(v, 4), currency="USD", ts_ms=t, fetched_ms=now_ms)


def account_id(aws: Aws) -> str:
    acct = _ACCOUNT.get(aws.profile)
    if acct:
        return acct
    d = aws.call(["sts", "get-caller-identity"])
    acct = str(d.get("Account") or "")
    if not re.fullmatch(r"\d{12}", acct):
        raise AwsError("BadOutput")
    _ACCOUNT[aws.profile] = acct
    return acct


def fetch_budget(aws: Aws, name: str, now_ms: int) -> dict:
    acct = account_id(aws)
    d = aws.call(["budgets", "describe-budget", "--account-id", acct, "--budget-name", name])
    b = d.get("Budget") or {}
    cs = b.get("CalculatedSpend") or {}
    act, fc, lim = cs.get("ActualSpend") or {}, cs.get("ForecastedSpend") or {}, b.get("BudgetLimit") or {}
    actual = _num(act.get("Amount"))
    if actual is None:
        raise AwsError("BadOutput")
    unit = str(act.get("Unit") or "USD")[:8]
    return dict(actual=round(actual, 4), forecast=_r(_num(fc.get("Amount"))), limit=_r(_num(lim.get("Amount"))),
                unit=unit if re.fullmatch(r"[A-Z]{3}", unit) else "USD", updated_ms=_ts_ms(b.get("LastUpdatedTime")),
                fetched_ms=now_ms)


def _r(v):
    return None if v is None else round(v, 4)


# --- state file -----------------------------------------------------------------------------------------------------

def cache_path() -> Path:
    return config.data_dir() / "cloud_cost.json"


def month_of(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m")   # the free tier's month (UTC)


def next_month_ms(now_ms: int) -> int:
    d = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)


def empty_state(now_ms: int) -> dict:
    return dict(v=1, month=month_of(now_ms), calls=dict(cloudwatch=0, budgets=0, sts=0), metric=None, budget=None,
                err=dict(metric=None, budget=None), next=dict(metric=0, budget=0), check=None)


def load_state(now_ms: int) -> dict:
    try:
        st = json.loads(cache_path().read_text(encoding="utf-8"))
        if not isinstance(st, dict) or st.get("v") != 1:
            raise ValueError
    except (OSError, ValueError):
        st = empty_state(now_ms)
    base = empty_state(now_ms)
    for k, v in base.items():
        if not isinstance(st.get(k), type(v)) and v is not None:
            st[k] = v
        st.setdefault(k, v)
    if st["month"] != month_of(now_ms):            # a new month: the counter starts again
        st["month"], st["calls"] = month_of(now_ms), dict(cloudwatch=0, budgets=0, sts=0)
    return st


def save_state(st: dict) -> None:
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    for _ in range(5):   # Windows: the status line may hold the file open for a moment
        try:
            tmp.replace(p)
            return
        except PermissionError:
            time.sleep(0.05)
    tmp.replace(p)


def settings_key(p: CloudParams) -> str:
    """Ties a passed check to the settings it checked, without storing the names."""
    return hashlib.sha256("\0".join((p.aws_path, p.aws_profile, p.budget_name)).encode()).hexdigest()[:16]


def _counter(st: dict, cap: int):
    def count(svc: str):
        if sum(st["calls"].values()) >= cap:
            raise AwsError("CallCap")
        st["calls"][SERVICE_KEY[svc]] = st["calls"].get(SERVICE_KEY[svc], 0) + 1
    return count


def backoff_ms(interval_ms: int, fails: int) -> int:
    return min(interval_ms * 2 ** max(0, fails), 24 * HOUR)


def expected_calls(p: CloudParams, days: int = 30) -> dict:
    m = round(days * 1440 / p.metric_min)
    b = round(days * 24 / p.budget_h) if p.budget_name else 0
    return dict(metric=m, budget=b, total=m + b)


def _apply(st: dict, kind: str, value: dict | None, code: str | None, now_ms: int, interval_ms: int) -> None:
    if code is None:
        st[kind] = value
        st["err"][kind] = None
        st["next"][kind] = now_ms + interval_ms
        return
    prev = st["err"].get(kind) or {}
    if code == "CallCap":
        st["err"][kind] = dict(code=code, at_ms=now_ms, fails=prev.get("fails", 0))
        st["next"][kind] = next_month_ms(now_ms)
        return
    fails = prev.get("fails", 0) + 1
    st["err"][kind] = dict(code=code, at_ms=now_ms, fails=fails)
    st["next"][kind] = now_ms + backoff_ms(interval_ms, fails)


def tick(now_ms: int, p: CloudParams | None = None, exe: list[str] | None = None, run=None) -> dict | None:
    """One refresher step. Disabled: returns None and starts nothing."""
    p = p or load_cloud_params()
    if not p.enabled:
        return None
    with LOCK:
        st = load_state(now_ms)
        due_m = now_ms >= st["next"].get("metric", 0)
        due_b = bool(p.budget_name) and now_ms >= st["next"].get("budget", 0)
        if not (due_m or due_b):
            return st
        path = resolve_aws(p.aws_path) if exe is None else "x"
        if path is None:
            for kind, due, iv in (("metric", due_m, p.metric_min * MIN), ("budget", due_b, p.budget_h * HOUR)):
                if due:
                    _apply(st, kind, None, "AwsNotFound", now_ms, iv)
            save_state(st)
            return st
        aws = Aws(exe or [path], p.aws_profile, _counter(st, p.max_calls_month), run)
        if due_m:
            try:
                _apply(st, "metric", fetch_metric(aws, now_ms), None, now_ms, p.metric_min * MIN)
            except AwsError as e:
                _apply(st, "metric", None, e.code, now_ms, p.metric_min * MIN)
        if due_b:
            try:
                _apply(st, "budget", fetch_budget(aws, p.budget_name, now_ms), None, now_ms, p.budget_h * HOUR)
            except AwsError as e:
                if e.code in ("AccessDenied", "ExpiredToken", "InvalidClientTokenId"):
                    _ACCOUNT.pop(p.aws_profile, None)
                _apply(st, "budget", None, e.code, now_ms, p.budget_h * HOUR)
        save_state(st)
        return st


def check(p: CloudParams, now_ms: int, exe: list[str] | None = None, run=None) -> dict:
    """`moni-token cloud check` and the dashboard's Check button: at most 3 free calls (sts, cloudwatch, budgets).
    A passed check also fills the cache, so turning the feature on right after it starts with fresh data."""
    steps = []
    path = resolve_aws(p.aws_path) if exe is None else " ".join(exe)
    steps.append(dict(id="aws", ok=path is not None, code=None if path else "AwsNotFound", path=path))
    with LOCK:
        st = load_state(now_ms)
        aws = Aws(exe or [path or "aws"], p.aws_profile, _counter(st, p.max_calls_month), run)
        ok_profile = False
        if path:
            _ACCOUNT.pop(p.aws_profile, None)
            try:
                account_id(aws)   # the ID stays in memory; it is not part of the result
                ok_profile = True
                steps.append(dict(id="profile", ok=True, code=None))
            except AwsError as e:
                steps.append(dict(id="profile", ok=False, code=e.code))
        else:
            steps.append(dict(id="profile", ok=False, code="Skipped"))
        metric = budget = None
        if ok_profile:
            try:
                metric = fetch_metric(aws, now_ms)
                steps.append(dict(id="metric", ok=True, code=None, value=metric))
            except AwsError as e:
                steps.append(dict(id="metric", ok=False, code=e.code))
            if p.budget_name:
                try:
                    budget = fetch_budget(aws, p.budget_name, now_ms)
                    steps.append(dict(id="budget", ok=True, code=None if budget["forecast"] is not None
                                      else "NoForecast", value=budget))
                except AwsError as e:
                    steps.append(dict(id="budget", ok=False, code=e.code))
            else:
                steps.append(dict(id="budget", ok=None, code="NoBudget"))
        else:
            steps += [dict(id="metric", ok=False, code="Skipped"), dict(id="budget", ok=False, code="Skipped")]
        ok = bool(path) and ok_profile and (metric is not None or budget is not None) and \
            not any(s["ok"] is False for s in steps if s["id"] == "budget")
        steps.append(dict(id="calls", ok=True, code=None, value=dict(expected_calls(p), cap=p.max_calls_month,
                                                                      used=dict(st["calls"]))))
        if metric:
            _apply(st, "metric", metric, None, now_ms, p.metric_min * MIN)
        if budget:
            _apply(st, "budget", budget, None, now_ms, p.budget_h * HOUR)
        st["check"] = dict(ok=ok, at_ms=now_ms, key=settings_key(p),
                           steps=[dict(id=s["id"], ok=s["ok"], code=s["code"]) for s in steps])
        save_state(st)
    return dict(ok=ok, at_ms=now_ms, steps=steps)


def check_passed(p: CloudParams, now_ms: int) -> bool:
    c = load_state(now_ms).get("check") or {}
    return bool(c.get("ok")) and c.get("key") == settings_key(p)


def view(p: CloudParams, now_ms: int, served: bool = False, token: str | None = None) -> dict:
    """What the usage page shows. The static report gets no settings and no token."""
    st = load_state(now_ms)
    c = st.get("check")
    v = dict(served=served, enabled=p.enabled, metric_min=p.metric_min, budget_h=p.budget_h, cap=p.max_calls_month,
             expected=expected_calls(p), calls=st["calls"], month=st["month"], metric=st["metric"],
             budget=st["budget"], err=st["err"], next=st["next"], stale_ms=STALE_MS,
             check=dict(ok=c.get("ok"), at_ms=c.get("at_ms"), matches=c.get("key") == settings_key(p),
                        steps=c.get("steps")) if isinstance(c, dict) else None)
    if served:
        v.update(token=token, policy=POLICY, settings=dict(aws_profile=p.aws_profile, budget_name=p.budget_name,
                                                           aws_path=p.aws_path),
                 aws_found=resolve_aws(p.aws_path))
    return v


def run_forever(stop: threading.Event, every_s: float = 30.0) -> None:
    """Inside `moni_token map` (a daemon thread): runs only while the map task runs. Reads config.toml each round,
    so the dashboard's on/off takes effect without a restart."""
    while not stop.is_set():
        try:
            tick(int(time.time() * 1000))
        except Exception:   # never take the map down
            pass
        stop.wait(every_s)
