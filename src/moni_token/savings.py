"""What could have been saved, per cause — a counterfactual recomputed from the same log numbers.

Deterministic: each cause has one concrete alternative and a formula. Every assumption the formula needs
(fresh-session context, size after /compact, how much of a big input was needed) is a parameter and is
spelled out in the sentence shown to the user. Parallel sessions, web research and output bursts get
advice only: the work itself was real, so no saving is claimed.
"""
from dataclasses import dataclass

from .units import price_for, table


@dataclass(frozen=True)
class SavingParams:
    fresh_ctx_tokens: int = 30_000       # context of a new session (system prompt, CLAUDE.md, short summary)
    compact_ctx_tokens: int = 80_000     # context left after /compact
    big_input_needed: float = 0.5        # share of a big file/result that was actually needed


def _unit(model) -> tuple[float, float, float, float]:
    """$ per token: input, cache read, cache write 5m, cache write 1h."""
    m = table()["multipliers"]
    pin = price_for(model).input / 1e6
    return pin, pin * m["cache_read"], pin * m["cache_write_5m"], pin * m["cache_write_1h"]


def _ctx(r) -> int:
    return r["input"] + r["cache_read"] + r["cache_5m"] + r["cache_1h"]


def idle_resume(rows, at_ms, sp: SavingParams) -> tuple[float, str, dict]:
    """Alternative: start a new session instead of resuming. The resume call writes only a fresh context,
    and every later call in the window reads (old context - fresh context) fewer tokens."""
    i = next(k for k, r in enumerate(rows) if r["ts_ms"] == at_ms)
    r = rows[i]
    _, pr, p5, p1 = _unit(r["model"])
    write = r["cache_5m"] + r["cache_1h"]
    pw = p1 if r["cache_1h"] >= r["cache_5m"] else p5
    excess = max(0, write - sp.fresh_ctx_tokens)
    later = len(rows) - i - 1
    saved = excess * pw + later * excess * pr
    return saved, "save.idle_resume", dict(write=write, fresh=sp.fresh_ctx_tokens, later=later,
                                           fresh_k=sp.fresh_ctx_tokens // 1000)


def long_context(rows, sp: SavingParams) -> tuple[float, str, dict]:
    """Alternative: /compact once at the start of the window, then every call reads at most compact_ctx."""
    t = sp.compact_ctx_tokens
    p1 = _unit(rows[0]["model"])[3]
    reads = sum(max(0, r["cache_read"] - t) * _unit(r["model"])[1] for r in rows)
    saved = max(0.0, reads - t * p1)          # minus re-caching the compacted context once
    med = sorted(_ctx(r) for r in rows)[len(rows) // 2]
    return saved, "save.long_context", dict(t_k=t // 1000, calls=len(rows), less=max(0, med - t))


def cache_miss(write_tokens, model, sp: SavingParams) -> tuple[float, str, dict]:
    """Alternative: the cache had stayed valid — the same tokens are read instead of written."""
    _, pr, _, p1 = _unit(model)
    return write_tokens * (p1 - pr), "save.cache_miss", dict(write=write_tokens)


def big_input(rows, big_rows, sp: SavingParams) -> tuple[float, str, dict]:
    """Alternative: read only the needed part. Input bytes / 4 ~ tokens; the unneeded share is neither
    written to cache nor re-read by later calls."""
    saved, total_tok = 0.0, 0
    for b in big_rows:
        tok = max(b["prev_result_bytes"], b["prev_attach_bytes"]) // 4
        unneeded = int(tok * (1 - sp.big_input_needed))
        _, pr, p5, p1 = _unit(b["model"])
        later = sum(1 for r in rows if r["ts_ms"] > b["ts_ms"])
        saved += unneeded * (p1 if b["cache_1h"] >= b["cache_5m"] else p5) + later * unneeded * pr
        total_tok += tok
    need = int(sp.big_input_needed * 100)
    return saved, "save.big_input", dict(tokens=total_tok, need=need)


# causes that get advice only: the catalog key of their explanation (issue #12)
ADVICE_ONLY = {"parallel": "save.parallel", "web_research": "save.web_research", "output_burst": "save.output_burst"}
