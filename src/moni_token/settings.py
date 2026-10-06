"""Detection thresholds. Defaults are the M0 draft; the user's confirmed values go in ~/.moni_token/config.toml.

Example config.toml:

    [spike]
    window_min = 15
    baseline_min = 60
    multiplier = 3.0
    floor_usd = 2.0
    single_call_cache_write_tokens = 200000
    realert_min = 30
    escalate_factor = 2.0
"""
import tomllib
from dataclasses import dataclass, fields

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


def load_spike_params(path=None) -> SpikeParams:
    p = path or (config.data_dir() / "config.toml")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8")).get("spike", {})
    except (OSError, tomllib.TOMLDecodeError):
        raw = {}
    known = {f.name for f in fields(SpikeParams)}
    return SpikeParams(**{k: v for k, v in raw.items() if k in known})
