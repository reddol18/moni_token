"""Turn fresh spikes into desktop notifications, once each. Text is a fixed template (no LLM)."""
import sqlite3
from datetime import datetime

from .notify import desktop_notify
from .settings import SpikeParams
from .spikes import Spike, detect

LOOKBACK_MS = 2 * 3_600_000
FRESH_MS = 15 * 60_000


def _hm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def render(s: Spike) -> tuple[str, str]:
    if s.kind == "cache_write":
        e = s.evidence
        idle = f", {e['idle_before_min']:.0f}분 쉰 뒤" if e.get("idle_before_min") else ""
        return ("Claude 사용량: 캐시 재기록",
                f"{_hm(s.start_ms)} {e['project']} 세션{idle} 한 번에 "
                f"{e['cache_write_tokens']:,}토큰 재기록(${s.usd:.2f}). 오래 쉰 큰 세션은 새 세션으로.")
    ratio = f"평소의 {s.ratio:.1f}배" if s.ratio else "쉬던 중 급증"
    return ("Claude 사용량 급증",
            f"{_hm(s.start_ms)}~{_hm(s.end_ms)} {s.evidence['window_min']}분에 ${s.usd:.2f} ({ratio}, "
            f"기준 ${s.evidence['threshold_usd']:.2f}). 원인: moni-token spikes")


def check_and_notify(con: sqlite3.Connection, now_ms: int, p: SpikeParams, notify=None) -> list[Spike]:
    notify = notify or desktop_notify
    sent = []
    for s in detect(con, now_ms - LOOKBACK_MS, now_ms, p):
        key = (s.kind, s.scope, s.start_ms)
        if con.execute("SELECT 1 FROM alerts WHERE kind=? AND scope=? AND start_ms=?", key).fetchone():
            continue
        # stale hits (first run, or after the PC slept) are recorded silently instead of a toast burst
        ok = notify(*render(s)) if s.end_ms >= now_ms - FRESH_MS else False
        con.execute("INSERT INTO alerts VALUES (?,?,?,?,?)", (*key, now_ms, int(ok)))
        if ok:
            sent.append(s)
    con.commit()
    return sent
