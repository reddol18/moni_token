"""Turn fresh spikes into desktop notifications, once each. Text is a fixed template (no LLM)."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .analysis import CODES, Analysis
from .events import record_spike
from .i18n import t, warning_text
from .notify import desktop_notify
from .settings import SpikeParams
from .spikes import Spike, detect

LOOKBACK_MS = 2 * 3_600_000
FRESH_MS = 15 * 60_000


def _hm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def render(s: Spike, a: Analysis | None = None, lang: str = "ko") -> tuple[str, str]:
    """Notification title and body. `a` should be analysed in the same `lang`."""
    if s.kind == "cache_write":
        e = s.evidence
        idle = t("notify.cw.idle", lang, idle=e["idle_before_min"]) if e.get("idle_before_min") else ""
        return (t("notify.cw.title", lang),
                t("notify.cw.body", lang, hm=_hm(s.start_ms), project=e["project"], idle=idle,
                  tokens=e["cache_write_tokens"]))
    if s.kind == "pct":
        e = s.evidence
        est = "" if e["basis"] == "measured" else t("notify.est", lang)
        who = ", ".join(f"{g['project']} {g['share']:.0%}" for g in (a.agents[:3] if a else []))
        cause = t("notify.pct.cause", lang, label=a.causes[0].label, advice=a.causes[0].advice) if a and a.causes else ""
        best = max((c for c in (a.causes if a else []) if c.saving_usd > 0), key=lambda c: c.saving_usd, default=None)
        if best:
            cause += t("notify.pct.save", lang, pp=e["rise_pp"] * best.saving_share)
        return (t("notify.pct.title", lang, rise=e["rise_pp"], est=est),
                t("notify.pct.body", lang, start=_hm(s.start_ms), end=_hm(s.end_ms), frm=e["from_pct"], to=e["to_pct"],
                  who=who, cause=cause))
    ratio = t("notify.rate.ratio", lang, ratio=s.ratio) if s.ratio else t("notify.rate.idle", lang)
    head = t("notify.rate.head", lang, start=_hm(s.start_ms), end=_hm(s.end_ms), usd=s.usd, ratio=ratio)
    if a and a.causes:
        c = a.causes[0]
        top = next((x for x in a.sessions if x["session_id"] == c.scope), None)
        where = f" {top['project']}" if top else ""
        return (t("notify.rate.title_cause", lang, label=c.label),
                t("notify.rate.body_cause", lang, head=head, where=where, fact=c.fact_text, advice=c.advice))
    return (t("notify.rate.title", lang), t("notify.rate.body", lang, head=head, threshold=s.evidence["threshold_usd"]))


def live_warning(con: sqlite3.Connection, now_ms: int, p: SpikeParams, lang: str = "ko") -> dict | None:
    """The spike to show on the status line: the latest event that ended within realert_min (issue #2).
    `text` is in `lang`; kind, rise_pp and code let the status line re-render it in its own language (issue #12)."""
    hold = p.realert_min * 60_000
    for kind, start, end, causes, spike in con.execute(
            "SELECT kind, start_ms, end_ms, causes_json, spike_json FROM events WHERE kind IN ('pct','cache_write') "
            "AND end_ms >= ? AND start_ms <= ? ORDER BY end_ms DESC LIMIT 1", (now_ms - hold, now_ms)):
        ev = (json.loads(spike or "null") or {}).get("evidence", {})
        code = (json.loads(causes) or [{}])[0].get("code")
        w = dict(kind=kind, rise_pp=ev.get("rise_pp"), code=code if code in CODES else None)
        return dict(text=warning_text(w, lang), until_ms=end + hold, **w)
    return None


def write_live(path: Path, w: dict | None) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(w, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def check_and_notify(con: sqlite3.Connection, now_ms: int, p: SpikeParams, notify=None,
                     lang: str = "ko") -> list[Spike]:
    notify = notify or desktop_notify
    sent = []
    for s in detect(con, now_ms - LOOKBACK_MS, now_ms, p):
        key = (s.kind, s.scope, s.start_ms)
        if con.execute("SELECT 1 FROM alerts WHERE kind=? AND scope=? AND start_ms=?", key).fetchone():
            continue
        _, a = record_spike(con, s, now_ms, lang=lang)
        # stale hits (first run, or after the PC slept) are recorded silently instead of a toast burst
        ok = notify(*render(s, a, lang)) if s.end_ms >= now_ms - FRESH_MS else False
        con.execute("INSERT INTO alerts VALUES (?,?,?,?,?)", (*key, now_ms, int(ok)))
        if ok:
            sent.append(s)
    con.commit()
    return sent
