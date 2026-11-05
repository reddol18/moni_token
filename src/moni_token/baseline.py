"""Baselines for the limit charts (issue #8): the usual curve, last week, a high/normal/low verdict and the pace.

Deterministic statistics over the same % series the charts draw (pctseries) — no new storage, no LLM.
"usual" = per offset into a window (5-minute steps), the P25/P50/P75 of every earlier window at that offset.
"""
import sqlite3

from .forecast import quantile
from .pctseries import WINDOW_MS, series

DAY = 86_400_000
HISTORY_DAYS = {"five_hour": 56, "seven_day": 91}
MIN_WINDOWS = 5
PACE_MS = {"five_hour": 3_600_000, "seven_day": DAY}   # "current pace" = slope over this much recent time


def value(p) -> float | None:
    """What the chart shows at a point: measured if present, else the estimate."""
    return p.measured if p.measured is not None else p.estimated


def window_curves(s: dict, before_ms: int) -> list[dict[int, float]]:
    """{offset step -> %} for every window in the series that ended by before_ms."""
    pts, step = s["points"], _step(s)
    if not pts:
        return []
    t0 = pts[0].t
    curves = []
    for a, b, *_ in s["windows"]:
        if b > before_ms or a < t0:
            continue
        c = {}
        i = max(0, -(-(a - t0) // step))   # first grid point at or after the window start
        while i < len(pts) and pts[i].t < b:
            v = value(pts[i])
            if v is not None:
                c[(pts[i].t - a) // step] = v
            i += 1
        if c and max(c.values()) > 0:   # a window with no usage at all (before the logs began) says nothing
            curves.append(c)
    return curves


def _step(s: dict) -> int:
    pts = s["points"]
    return pts[1].t - pts[0].t if len(pts) > 1 else 300_000


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


def usual(curves: list[dict[int, float]], k: int) -> dict | None:
    vs = [c[k] for c in curves if k in c]
    if len(vs) < MIN_WINDOWS:
        return dict(n=len(vs)) if vs else None
    return dict(n=len(vs), p25=quantile(vs, .25), p50=quantile(vs, .5), p75=quantile(vs, .75))


def verdict(cur: float | None, u: dict | None) -> str:
    if cur is None or not u or "p50" not in u:
        return "unknown"
    if cur > u["p75"]:
        return "high"
    if cur < u["p25"]:
        return "low"
    return "normal"


def pace(s: dict, kind: str, now_ms: int, cur: float | None, win: tuple | None) -> dict | None:
    """Projected % at the reset if the recent slope continues, and when 100% would come first."""
    if cur is None or not win:
        return None
    a, b = win[0], win[1]
    since = max(a, now_ms - PACE_MS[kind])
    then = None
    for p in s["points"]:
        if p.t > since:
            break
        then = p
    v0 = value(then) if then and then.t >= a else 0.0
    t0 = then.t if then and then.t >= a else a
    if v0 is None or now_ms <= t0:
        return None
    rate = max(0.0, (cur - v0) / (now_ms - t0))   # % per ms
    proj = cur + rate * (b - now_ms)
    full = now_ms + int((100 - cur) / rate) if rate > 0 and proj >= 100 and cur < 100 else None
    return dict(at_reset=round(min(proj, 999), 1), full_ms=full, per_hour=round(rate * 3_600_000, 2))


def baseline(con: sqlite3.Connection, kind: str, chart: dict, now_ms: int) -> dict:
    """Arrays aligned with the chart's points (`chart` = pctseries.series over the chart range) plus the verdict."""
    hist = series(con, kind, chart["points"][0].t - HISTORY_DAYS[kind] * DAY, now_ms) if chart["points"] \
        else series(con, kind, now_ms - HISTORY_DAYS[kind] * DAY, now_ms)
    win = next((w for w in chart["windows"] if w[0] <= now_ms < w[1]), None)
    curves_all = window_curves(hist, now_ms)
    step = _step(hist)
    by_t = {p.t: p for p in hist["points"]}
    p25, p50, p75, last = [], [], [], []
    memo: dict[int, dict | None] = {}
    wi = 0
    wins = chart["windows"]
    for p in chart["points"]:
        while wi < len(wins) and wins[wi][1] <= p.t:
            wi += 1
        w = wins[wi] if wi < len(wins) and wins[wi][0] <= p.t else None
        u = None
        if w:
            k = (p.t - w[0]) // step
            if k not in memo:
                memo[k] = usual(curves_all, k)
            u = memo[k]
        ok = u and "p50" in u
        p25.append(_r(u["p25"]) if ok else None)
        p50.append(_r(u["p50"]) if ok else None)
        p75.append(_r(u["p75"]) if ok else None)
        lw = by_t.get(p.t - 7 * DAY)
        last.append(_r(value(lw)) if lw else None)
    cur = value(chart["points"][-1]) if chart["points"] else None
    u_now = usual(curves_all, (now_ms - win[0]) // step) if win else None
    return dict(p25=p25, p50=p50, p75=p75, last_week=last, windows_n=len(curves_all),
                verdict=verdict(cur, u_now), now=dict(cur=cur, offset_ms=(now_ms - win[0]) if win else None,
                                                      **(u_now or {})),
                pace=pace(chart, kind, now_ms, cur, win), window_ms=WINDOW_MS[kind])
