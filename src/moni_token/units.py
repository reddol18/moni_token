"""Usage unit = API list-price USD (ADR-0002). Pure arithmetic over a bundled price table."""
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    estimated: bool


@lru_cache(maxsize=1)
def table() -> dict:
    return tomllib.loads(resources.files("moni_token").joinpath("pricing.toml").read_text(encoding="utf-8"))


@lru_cache(maxsize=256)
def price_for(model: str | None) -> Price:
    t = table()
    models = t["models"]
    m = re.sub(r"\[.*?\]$", "", model or "")      # "claude-opus-5[1m]" -> "claude-opus-5"
    m = re.sub(r"-\d{8}$", "", m)                 # drop date suffix
    if m in models:
        p = models[m]
        return Price(p["input"], p["output"], not p.get("verified", False))
    for fam, fallback in t["families"].items():
        if fam in m:
            p = models[fallback]
            return Price(p["input"], p["output"], True)
    p = models[t["families"]["opus"]]             # unknown family: assume the expensive one
    return Price(p["input"], p["output"], True)


def call_usd(model, input, output, cache_read, cache_5m, cache_1h, web_search_n=0) -> float:
    t = table()
    mul = t["multipliers"]
    p = price_for(model)
    return (p.input * (input + mul["cache_read"] * cache_read + mul["cache_write_5m"] * cache_5m
                       + mul["cache_write_1h"] * cache_1h) + p.output * output) / 1e6 \
        + web_search_n * t["web_search"]["usd_per_request"]


def usd_parts(model, input, output, cache_read, cache_5m, cache_1h, web_search_n=0) -> dict:
    """Cost split by token kind — the evidence behind every cause verdict."""
    t = table()
    mul = t["multipliers"]
    p = price_for(model)
    return {
        "input": p.input * input / 1e6,
        "output": p.output * output / 1e6,
        "cache_read": p.input * mul["cache_read"] * cache_read / 1e6,
        "cache_write_5m": p.input * mul["cache_write_5m"] * cache_5m / 1e6,
        "cache_write_1h": p.input * mul["cache_write_1h"] * cache_1h / 1e6,
        "web_search": web_search_n * t["web_search"]["usd_per_request"],
    }
