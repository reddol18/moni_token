"""Cause analysis for a time window (PLAN §4.3). Rules and templates only — no LLM.

Facts (counts, sizes, dollars straight from the logs) are kept apart from the interpretation sentence,
which is always labelled as an estimate (PLAN §2.4).
"""
import sqlite3
import statistics
from dataclasses import dataclass, field

from . import savings as sv
from .units import usd_parts

MIN = 60_000
OUTPUT_NOTE = "output 토큰은 로그 특성상 과소일 수 있음"


@dataclass(frozen=True)
class CauseParams:
    long_ctx_tokens: int = 200_000        # median context per call
    long_ctx_min_calls: int = 20
    long_ctx_read_share: float = 0.4      # cache_read share of the session's cost
    idle_min_1h: float = 60.0             # cache TTLs
    idle_min_5m: float = 5.0
    idle_min_write_tokens: int = 100_000
    cache_write_share: float = 0.4
    big_result_bytes: int = 100_000
    big_attach_bytes: int = 100_000
    parallel_sessions: int = 3            # sessions with >= parallel_min_calls in the window
    parallel_min_calls: int = 3
    web_calls: int = 10
    output_share: float = 0.4
    top_share: float = 0.8                # explain sessions until this share of the window's cost


TEMPLATES = {
    "idle_resume": ("공백 후 재개(캐시 만료)",
                    "{idle:.0f}분 쉰 뒤 첫 호출이 컨텍스트 {write:,}토큰을 캐시에 다시 썼다(이 구간 사용량의 {wshare:.0%}).",
                    "캐시 TTL({ttl})이 지난 큰 대화를 이어 쓰면 대화 전체를 다시 캐시에 쓴다.",
                    "1시간 넘게 쉰 큰 세션은 이어쓰지 말고 새 세션으로 시작하세요."),
    "long_context": ("긴 대화 × 잦은 호출",
                     "{calls}회 호출, 호출당 컨텍스트 중앙값 {ctx:,}토큰(최대 {ctx_max:,}), 캐시 읽기가 세션 사용량의 "
                     "{share:.0%}.",
                     "호출마다 긴 대화 전체를 다시 읽어 호출 수만큼 비용이 쌓였다.",
                     "/compact 하거나 새 세션에서 이어가고, 반복 작업은 스크립트로 묶으세요."),
    "cache_miss": ("캐시 미스",
                   "캐시 쓰기 {write:,}토큰(세션 사용량의 {share:.0%}), 공백 없는 재기록.",
                   "모델·설정·컨텍스트 변경 등으로 캐시가 무효화된 것으로 보인다.",
                   "작업 중 모델·설정 전환을 줄이세요."),
    "big_input": ("큰 입력",
                  "직전 입력 최대 {bytes:,}바이트(이미지 {images}건), 뒤이은 캐시 쓰기 {write:,}토큰.",
                  "큰 파일·이미지·첨부가 컨텍스트에 들어가 이후 호출이 모두 무거워졌다.",
                  "큰 파일은 필요한 부분만 읽고, 원문은 파일로 넘기세요."),
    "parallel": ("서브에이전트·병렬 세션",
                 "동시 활동 세션 {n}개(프로젝트 {projects}), 사이드체인 호출 {side}회, headless {sdk}회.",
                 "여러 세션이 같은 한도를 동시에 썼다.",
                 "동시에 돌리는 세션 수를 줄이거나 순차로 돌리세요."),
    "web_research": ("웹 조사 다수",
                     "웹 도구 {n}회(WebSearch/WebFetch), 서버 웹검색 {ws}회.",
                     "웹 결과가 컨텍스트에 쌓였다.",
                     "조사 범위를 좁히고 결과는 요약만 남기세요."),
    "output_burst": ("출력 폭주",
                     "output {tokens:,}토큰(세션 사용량의 {share:.0%}).",
                     "긴 생성이 비용의 큰 몫을 차지했다.",
                     "긴 결과물은 파일로 쓰게 하고 출력 길이를 제한하세요."),
}


@dataclass
class Cause:
    code: str
    scope: str                 # session id or "all"
    usd: float                 # cost attributed to this cause
    facts: dict
    label: str = ""
    fact_text: str = ""
    interpretation: str = ""   # always an estimate
    advice: str = ""
    share: float = 0.0         # attributed usage / the window's usage
    saving_usd: float = 0.0    # counterfactual saving under `alternative` (savings.py)
    alternative: str = ""      # the alternative, with its assumptions spelled out
    saving_share: float = 0.0  # saving / the window's usage


@dataclass
class Analysis:
    start_ms: int
    end_ms: int
    usd: float
    calls: int
    sessions: list[dict] = field(default_factory=list)
    causes: list[Cause] = field(default_factory=list)
    agents: list[dict] = field(default_factory=list)   # per project folder: share of the window's usage
    note: str = OUTPUT_NOTE

    def headline(self) -> str:
        if not self.causes:
            return "뚜렷한 원인 규칙 없음"
        c = self.causes[0]
        return f"{c.label} (구간의 {c.usd / self.usd:.0%})" if self.usd else c.label


def _make(code, scope, usd, facts, /, **fmt) -> Cause:
    label, fact_t, interp, advice = TEMPLATES[code]
    return Cause(code, scope, usd, facts, label, fact_t.format(**fmt), interp.format(**fmt) + " (추정)", advice)


def _save(c: Cause, result: tuple[float, str]) -> None:
    c.saving_usd, c.alternative = round(result[0], 4), result[1]


def _session_causes(rows, prev_ts, p: CauseParams, window_total: float = 0.0,
                    sp: sv.SavingParams = sv.SavingParams()) -> list[Cause]:
    sid = rows[0]["session_id"]
    parts = {k: sum(r["parts"][k] for r in rows) for k in rows[0]["parts"]}
    total = sum(parts.values()) or 1e-12
    out: list[Cause] = []

    # 1) idle resume: first call after a gap longer than the cache TTL re-writes the whole context
    idle_ids = set()
    last = prev_ts
    for r in rows:
        if last is not None:
            idle = (r["ts_ms"] - last) / MIN
            write = r["cache_5m"] + r["cache_1h"]
            ttl = p.idle_min_1h if r["cache_1h"] >= r["cache_5m"] else p.idle_min_5m
            if idle >= ttl and write >= p.idle_min_write_tokens:
                idle_ids.add(r["msg_id"])
                usd = r["parts"]["cache_write_1h"] + r["parts"]["cache_write_5m"]
                out.append(_make("idle_resume", sid, usd,
                                 dict(idle_min=round(idle, 1), cache_write_tokens=write, usd=round(usd, 4),
                                      ttl="1h" if ttl == p.idle_min_1h else "5m", at_ms=r["ts_ms"]),
                                 idle=idle, write=write, wshare=usd / (window_total or total),
                                 ttl="1시간" if ttl == p.idle_min_1h else "5분"))
                _save(out[-1], sv.idle_resume(rows, r["ts_ms"], sp))
        last = r["ts_ms"]

    # 2) long context x many calls
    ctx = [r["input"] + r["cache_read"] + r["cache_5m"] + r["cache_1h"] for r in rows]
    med = int(statistics.median(ctx))
    read_share = parts["cache_read"] / total
    if med >= p.long_ctx_tokens and len(rows) >= p.long_ctx_min_calls and read_share >= p.long_ctx_read_share:
        usd = parts["cache_read"] + parts["input"]
        out.append(_make("long_context", sid, usd,
                         dict(calls=len(rows), ctx_median=med, ctx_max=max(ctx), cache_read_usd=round(parts["cache_read"], 4),
                              cache_read_share=round(read_share, 3)),
                         calls=len(rows), ctx=med, ctx_max=max(ctx), read=parts["cache_read"], share=read_share))
        _save(out[-1], sv.long_context(rows, sp))

    # 3) cache miss not explained by idle resume
    other = [r for r in rows if r["msg_id"] not in idle_ids]
    w_usd = sum(r["parts"]["cache_write_5m"] + r["parts"]["cache_write_1h"] for r in other)
    w_tok = sum(r["cache_5m"] + r["cache_1h"] for r in other)

    # 4) big input precedes the writes
    big = [r for r in other if r["prev_result_bytes"] >= p.big_result_bytes or r["prev_result_image"]
           or r["prev_attach_bytes"] >= p.big_attach_bytes]
    if big:
        b_write = sum(r["cache_5m"] + r["cache_1h"] for r in big)
        b_usd = sum(r["parts"]["cache_write_5m"] + r["parts"]["cache_write_1h"] for r in big)
        if b_usd / total >= 0.1:
            mx = max(max(r["prev_result_bytes"], r["prev_attach_bytes"]) for r in big)
            imgs = sum(r["prev_result_image"] for r in big)
            out.append(_make("big_input", sid, b_usd, dict(max_bytes=mx, images=imgs, cache_write_tokens=b_write),
                             bytes=mx, images=imgs, write=b_write))
            _save(out[-1], sv.big_input(rows, big, sp))
            w_usd -= b_usd
            w_tok -= b_write
    if w_usd / total >= p.cache_write_share:
        out.append(_make("cache_miss", sid, w_usd, dict(cache_write_tokens=w_tok, share=round(w_usd / total, 3)),
                         write=w_tok, usd=w_usd, share=w_usd / total))
        _save(out[-1], sv.cache_miss(w_tok, rows[0]["model"], sp))

    # 5) web research
    web = sum(r["web_tools"] for r in rows)
    ws = sum(r["web_search_n"] for r in rows)
    if web + ws >= p.web_calls:
        out.append(_make("web_research", sid, parts["web_search"], dict(web_tool_calls=web, server_web_search=ws),
                         n=web, ws=ws))

    # 6) output burst
    if parts["output"] / total >= p.output_share:
        tok = sum(r["output"] for r in rows)
        out.append(_make("output_burst", sid, parts["output"],
                         dict(output_tokens=tok, output_usd=round(parts["output"], 4), note=OUTPUT_NOTE),
                         usd=parts["output"], share=parts["output"] / total, tokens=tok))
    return out


def analyze(con: sqlite3.Connection, start_ms: int, end_ms: int, p: CauseParams = CauseParams()) -> Analysis:
    cols = ("msg_id ts_ms session_id project_dir project agent_id is_sidechain entrypoint model input output "
            "cache_read cache_5m cache_1h web_search_n prev_result_bytes prev_result_image prev_attach_bytes").split()
    rows = [dict(zip(cols, r)) for r in con.execute(
        f"SELECT {','.join(cols)} FROM calls WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms", (start_ms, end_ms))]
    web_by_msg = dict(con.execute(
        "SELECT t.msg_id, COUNT(*) FROM call_tools t JOIN calls c USING(msg_id) WHERE c.ts_ms >= ? AND c.ts_ms < ? "
        "AND t.name IN ('WebSearch','WebFetch') GROUP BY t.msg_id", (start_ms, end_ms)))
    for r in rows:
        r["parts"] = usd_parts(r["model"], r["input"], r["output"], r["cache_read"], r["cache_5m"], r["cache_1h"],
                               r["web_search_n"])
        r["usd"] = sum(r["parts"].values())
        r["web_tools"] = web_by_msg.get(r["msg_id"], 0)
    total = sum(r["usd"] for r in rows)
    a = Analysis(start_ms, end_ms, total, len(rows))
    if not rows:
        return a

    by_sess: dict[str, list] = {}
    for r in rows:
        by_sess.setdefault(r["session_id"] or "?", []).append(r)
    ranked = sorted(by_sess.items(), key=lambda kv: -sum(r["usd"] for r in kv[1]))
    acc = 0.0
    for sid, rs in ranked:
        s_usd = sum(r["usd"] for r in rs)
        a.sessions.append(dict(session_id=sid, project=rs[0]["project"] or rs[0]["project_dir"],
                               project_dir=rs[0]["project_dir"], calls=len(rs), usd=round(s_usd, 4),
                               share=round(s_usd / total, 3) if total else 0,
                               sidechain_calls=sum(r["is_sidechain"] for r in rs)))
        if acc / (total or 1) >= p.top_share:
            continue
        acc += s_usd
        prev = con.execute("SELECT MAX(ts_ms) FROM calls WHERE session_id = ? AND ts_ms < ?",
                           (sid, start_ms)).fetchone()[0]
        a.causes += _session_causes(rs, prev, p, total)

    # agent = project folder (subagent / headless calls fold into their project, shown as sub-shares)
    by_proj: dict[str, list] = {}
    for r in rows:
        by_proj.setdefault(r["project"] or r["project_dir"], []).append(r)
    for name, rs in sorted(by_proj.items(), key=lambda kv: -sum(r["usd"] for r in kv[1])):
        u = sum(r["usd"] for r in rs) or 1e-12
        a.agents.append(dict(
            project=name, calls=len(rs), sessions=len({r["session_id"] for r in rs}),
            share=round(sum(r["usd"] for r in rs) / total, 4) if total else 0,
            subagent_share=round(sum(r["usd"] for r in rs if r["is_sidechain"]) / u, 3),
            headless_share=round(sum(r["usd"] for r in rs if r["entrypoint"] == "sdk-cli") / u, 3)))

    # window-level: parallel sessions / subagents / headless
    active = [s for s in a.sessions if s["calls"] >= p.parallel_min_calls]
    side = sum(r["is_sidechain"] for r in rows)
    sdk = sum(1 for r in rows if r["entrypoint"] == "sdk-cli")
    if len(active) >= p.parallel_sessions or side >= p.parallel_min_calls * p.parallel_sessions or sdk:
        projects = sorted({s["project"] for s in active})
        non_top = sum(s["usd"] for s in active[1:])
        a.causes.append(_make("parallel", "all", non_top,
                              dict(active_sessions=len(active), projects=projects, sidechain_calls=side, sdk_calls=sdk),
                              n=len(active), projects=", ".join(projects), side=side, sdk=sdk))
    for c in a.causes:
        c.share = round(c.usd / total, 3) if total else 0.0
        c.saving_share = round(min(c.saving_usd, total) / total, 3) if total else 0.0
        if c.code in sv.ADVICE_ONLY:
            c.alternative = sv.ADVICE_ONLY[c.code]
    a.causes.sort(key=lambda c: -c.usd)
    return a
