"""Detection thresholds. Defaults are the M0 draft; the user's confirmed values go in ~/.moni_token/config.toml.

Example config.toml:

    lang = "en"                 # pages, notifications, status line and CLI (issue #12); en | ko

    [spike]
    window_min = 15
    baseline_min = 60
    multiplier = 3.0
    floor_usd = 2.0
    single_call_cache_write_tokens = 200000
    realert_min = 30
    escalate_factor = 2.0

    [map]
    uia = false                 # read the project's running window via Windows UI Automation (ADR-0007)
    uia_window = ""             # else: a window whose title contains this

    [cloud]                     # AWS cost in the status line (issue #18, ADR-0008); off by default
    enabled = false
    aws_profile = "moni-token-billing"
    budget_name = "monthly"
    aws_path = ""               # optional: full path to aws.exe; else PATH, then the usual install folders
    metric_min = 10             # CloudWatch EstimatedCharges every N minutes (>= 5)
    budget_h = 6                # Budgets DescribeBudget every N hours (>= 1)
    max_calls_month = 6000      # all calls stop past this until next month
"""
import json
import re
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

from . import config


@dataclass(frozen=True)
class SpikeParams:
    window_min: int = 15            # recent rate window
    baseline_min: int = 60          # baseline = median 5-min bucket over this span, before the window
    multiplier: float = 3.0         # window >= multiplier x baseline
    floor_usd: float = 2.0          # absolute floor per window (DRAFT until user confirms; see `moni-token suggest-floor`)
    single_call_cache_write_tokens: int = 200_000   # one call re-caching this much is an event by itself
    realert_min: int = 30
    escalate_factor: float = 2.0    # re-alert inside realert_min only if ratio grew this much
    pct_jump: float = 3.0           # 5h limit rising this many %p within window_min is an event (ADR-0003)


@dataclass(frozen=True)
class MapParams:
    """[map] in config.toml (issue #16, ADR-0007). `uia` turns on reading the project's running window through
    Windows UI Automation; `uia_window` picks a window by title when no process runs from the project folder."""
    uia: bool = False
    uia_window: str = ""


def load_map_params(path=None) -> MapParams:
    p = path or (config.data_dir() / "config.toml")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8")).get("map", {})
    except (OSError, tomllib.TOMLDecodeError):
        raw = {}
    return MapParams(uia=raw.get("uia") is True, uia_window=str(raw.get("uia_window") or ""))


@dataclass(frozen=True)
class CloudParams:
    """[cloud] in config.toml (issue #18, ADR-0008). config.toml is the source of truth; the dashboard writes it."""
    enabled: bool = False
    aws_profile: str = ""
    budget_name: str = ""
    aws_path: str = ""
    metric_min: int = 10
    budget_h: int = 6
    max_calls_month: int = 6000


PROFILE_RX = re.compile(r"[A-Za-z0-9_.+=,@-]{0,64}")
BUDGET_RX = re.compile(r"[A-Za-z0-9 _.+=,@()-]{0,100}")   # a safe subset of what AWS allows (no ':' or '\')
EDITABLE = ("enabled", "aws_profile", "budget_name", "aws_path")


def _int(v, default: int, lo: int, hi: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        return default
    return max(lo, min(hi, v))


def load_cloud_params(path=None) -> CloudParams:
    p = path or (config.data_dir() / "config.toml")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8")).get("cloud", {})
    except (OSError, tomllib.TOMLDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    s = lambda k: raw.get(k) if isinstance(raw.get(k), str) else ""
    return CloudParams(enabled=raw.get("enabled") is True, aws_profile=s("aws_profile"), budget_name=s("budget_name"),
                       aws_path=s("aws_path"), metric_min=_int(raw.get("metric_min"), 10, 5, 1440),
                       budget_h=_int(raw.get("budget_h"), 6, 1, 168),
                       max_calls_month=_int(raw.get("max_calls_month"), 6000, 0, 900_000))


def validate_cloud_fields(d) -> tuple[dict, list[str]]:
    """Dashboard input: only the known fields, each checked. Returns (clean values, names of bad fields)."""
    if not isinstance(d, dict):
        return {}, ["body"]
    bad = [k for k in d if k not in EDITABLE]
    out = {}
    for k, v in d.items():
        if k not in EDITABLE:
            continue
        if k == "enabled":
            (out.__setitem__(k, v) if isinstance(v, bool) else bad.append(k))
            continue
        if not isinstance(v, str):
            bad.append(k)
            continue
        v = v.strip()
        if k == "aws_profile" and PROFILE_RX.fullmatch(v):
            out[k] = v
        elif k == "budget_name" and BUDGET_RX.fullmatch(v):
            out[k] = v
        elif k == "aws_path" and _aws_path_ok(v):
            out[k] = v
        else:
            bad.append(k)
    return out, bad


def _aws_path_ok(v: str) -> bool:
    if v == "":
        return True
    if len(v) > 260 or any(c in v for c in "\r\n\0\"'"):
        return False
    p = Path(v)
    return p.is_absolute() and p.name.lower() in ("aws.exe", "aws") and p.is_file()


def _toml_str(s: str) -> str:
    return json.dumps(s)   # a JSON string with ASCII escapes is a valid TOML basic string


def save_cloud_params(p: CloudParams, path=None) -> Path:
    """Rewrite only the [cloud] table of config.toml; every other line (comments included) stays as it is."""
    path = path or (config.data_dir() / "config.toml")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    section = ["[cloud]   # AWS cost (issue #18, ADR-0008); written by the dashboard",
               f"enabled = {'true' if p.enabled else 'false'}", f"aws_profile = {_toml_str(p.aws_profile)}",
               f"budget_name = {_toml_str(p.budget_name)}", f"aws_path = {_toml_str(p.aws_path)}",
               f"metric_min = {int(p.metric_min)}", f"budget_h = {int(p.budget_h)}",
               f"max_calls_month = {int(p.max_calls_month)}"]
    lines = text.splitlines()
    head = re.compile(r"^\s*\[\s*cloud\s*\]")
    i = next((n for n, ln in enumerate(lines) if head.match(ln)), None)
    if i is None:
        new = lines + ([""] if lines and lines[-1].strip() else []) + section
    else:
        j = next((n for n in range(i + 1, len(lines)) if lines[n].lstrip().startswith("[")), len(lines))
        while j > i + 1 and (not lines[j - 1].strip() or lines[j - 1].lstrip().startswith("#")):   # the next table's
            j -= 1
        new = lines[:i] + section + lines[j:]
    out = "\n".join(new) + "\n"
    tomllib.loads(out)                                    # never write a file we could not read back
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(out, encoding="utf-8")
    tmp.replace(path)
    if load_cloud_params(path) != p:
        raise ValueError("config round-trip mismatch")
    return path


def update_cloud_params(values: dict, path=None) -> CloudParams:
    p = replace(load_cloud_params(path), **{k: v for k, v in values.items() if k in EDITABLE})
    save_cloud_params(p, path)
    return p


def load_spike_params(path=None) -> SpikeParams:
    p = path or (config.data_dir() / "config.toml")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8")).get("spike", {})
    except (OSError, tomllib.TOMLDecodeError):
        raw = {}
    known = {f.name for f in fields(SpikeParams)}
    return SpikeParams(**{k: v for k, v in raw.items() if k in known})
