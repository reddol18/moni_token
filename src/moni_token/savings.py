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


def idle_resume(rows, at_ms, sp: SavingParams) -> tuple[float, str]:
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
    return saved, (f"새 세션으로 시작했다면 재기록 {write:,}토큰 대신 약 {sp.fresh_ctx_tokens:,}토큰만 쓰고, 이후 {later}회 "
                   f"호출도 그만큼 덜 읽었을 것 (새 세션 컨텍스트 {sp.fresh_ctx_tokens // 1000}k 가정).")


def long_context(rows, sp: SavingParams) -> tuple[float, str]:
    """Alternative: /compact once at the start of the window, then every call reads at most compact_ctx."""
    t = sp.compact_ctx_tokens
    p1 = _unit(rows[0]["model"])[3]
    reads = sum(max(0, r["cache_read"] - t) * _unit(r["model"])[1] for r in rows)
    saved = max(0.0, reads - t * p1)          # minus re-caching the compacted context once
    med = sorted(_ctx(r) for r in rows)[len(rows) // 2]
    return saved, (f"구간 초입에 /compact로 컨텍스트를 약 {t // 1000}k로 줄였다면, 호출 {len(rows)}회가 각각 "
                   f"약 {max(0, med - t):,}토큰씩 덜 읽었을 것 (compact 후 {t // 1000}k 가정).")


def cache_miss(write_tokens, model, sp: SavingParams) -> tuple[float, str]:
    """Alternative: the cache had stayed valid — the same tokens are read instead of written."""
    _, pr, _, p1 = _unit(model)
    return write_tokens * (p1 - pr), (f"캐시가 유지됐다면 {write_tokens:,}토큰을 다시 쓰는 대신 읽기만 했을 것 "
                                      f"(작업 중 모델·설정 전환을 피한 경우).")


def big_input(rows, big_rows, sp: SavingParams) -> tuple[float, str]:
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
    return saved, (f"큰 입력(약 {total_tok:,}토큰) 중 필요한 부분만 읽었다면 나머지가 캐시와 이후 호출에서 빠졌을 것 "
                   f"(필요 비율 {need}% 가정, 4바이트≈1토큰).")


ADVICE_ONLY = {
    "parallel": "동시에 돌린 작업량 자체는 같으므로 절약액은 없음. 순차로 돌리면 급상승(한도 도달 속도)만 완화된다.",
    "web_research": "조사 결과가 컨텍스트에 쌓인 몫은 로그만으로 분리할 수 없어 절약액을 계산하지 않음. "
                    "조사는 짧은 별도 세션에서 하고 결론만 옮기면 본 대화가 가벼워진다.",
    "output_burst": "생성한 결과물 자체가 목적이었을 수 있어 절약액을 계산하지 않음. 긴 결과물은 파일로 쓰게 하라.",
}
