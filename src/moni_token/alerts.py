"""Turn fresh spikes into desktop notifications, once each. Text is a fixed template (no LLM)."""
import sqlite3
from datetime import datetime

from .analysis import Analysis
from .events import record_spike
from .notify import desktop_notify
from .settings import SpikeParams
from .spikes import Spike, detect

LOOKBACK_MS = 2 * 3_600_000
FRESH_MS = 15 * 60_000


def _hm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def render(s: Spike, a: Analysis | None = None) -> tuple[str, str]:
    if s.kind == "cache_write":
        e = s.evidence
        idle = f", {e['idle_before_min']:.0f}분 쉰 뒤" if e.get("idle_before_min") else ""
        return ("Claude 사용량: 캐시 재기록",
                f"{_hm(s.start_ms)} {e['project']} 세션{idle} 한 번에 "
                f"{e['cache_write_tokens']:,}토큰 재기록. 1시간 넘게 쉰 큰 세션은 새 세션으로.")
    if s.kind == "pct":
        e = s.evidence
        est = "" if e["basis"] == "measured" else " (추정)"
        who = ", ".join(f"{g['project']} {g['share']:.0%}" for g in (a.agents[:3] if a else []))
        cause = f" 원인: {a.causes[0].label}. {a.causes[0].advice}" if a and a.causes else ""
        best = max((c for c in (a.causes if a else []) if c.saving_usd > 0), key=lambda c: c.saving_usd, default=None)
        if best:
            cause += f" 절약 가능 ≈{e['rise_pp'] * best.saving_share:.1f}%p."
        return (f"현재 세션 한도 +{e['rise_pp']:.1f}%p{est}",
                f"{_hm(s.start_ms)}~{_hm(s.end_ms)} {e['from_pct']:.0f}% → {e['to_pct']:.0f}%. {who}.{cause}")
    ratio = f"평소의 {s.ratio:.1f}배" if s.ratio else "쉬던 중 급증"
    head = f"{_hm(s.start_ms)}~{_hm(s.end_ms)} ${s.usd:.2f} ({ratio})."
    if a and a.causes:
        c = a.causes[0]
        top = next((x for x in a.sessions if x["session_id"] == c.scope), None)
        where = f" {top['project']}" if top else ""
        return (f"Claude 사용량 급증: {c.label}", f"{head}{where} — {c.fact_text} {c.advice}")
    return ("Claude 사용량 급증", f"{head} 기준 ${s.evidence['threshold_usd']:.2f}. 상세: moni-token events")


def check_and_notify(con: sqlite3.Connection, now_ms: int, p: SpikeParams, notify=None) -> list[Spike]:
    notify = notify or desktop_notify
    sent = []
    for s in detect(con, now_ms - LOOKBACK_MS, now_ms, p):
        key = (s.kind, s.scope, s.start_ms)
        if con.execute("SELECT 1 FROM alerts WHERE kind=? AND scope=? AND start_ms=?", key).fetchone():
            continue
        _, a = record_spike(con, s, now_ms)
        # stale hits (first run, or after the PC slept) are recorded silently instead of a toast burst
        ok = notify(*render(s, a)) if s.end_ms >= now_ms - FRESH_MS else False
        con.execute("INSERT INTO alerts VALUES (?,?,?,?,?)", (*key, now_ms, int(ok)))
        if ok:
            sent.append(s)
    con.commit()
    return sent
