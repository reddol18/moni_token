"""Limit usage in % over time — what `/usage` shows as "current session" (5h) and "current week" (7d).

measured  : the status line's used_percentage (step function; drops to 0 when the window resets)
estimated : local log usage since the window start / calibrated usage-$ per 1% (dashed, labelled)
"""
import sqlite3
from dataclasses import dataclass

from .blocks import compute_blocks
from .buckets import BUCKET_MS
from .calibrate import estimate

WINDOW_MS = {"five_hour": 5 * 3_600_000, "seven_day": 7 * 86_400_000}


@dataclass
class Point:
    t: int
    measured: float | None
    estimated: float | None


def _measured_steps(con, kind):
    return con.execute("SELECT ts_ms, resets_at_ms, used_pct FROM limit_obs WHERE source IN ('statusline','usage') "
                       "AND kind=? AND used_pct IS NOT NULL ORDER BY ts_ms", (kind,)).fetchall()


def _week_windows(con, start, end):
    """Weekly windows are only known from observed reset times; extend them backwards/forwards by 7 days."""
    resets = sorted({r - r % 60_000 for (r,) in con.execute(
        "SELECT resets_at_ms FROM limit_obs WHERE kind='seven_day'")})
    if not resets:
        return []
    w = WINDOW_MS["seven_day"]
    r = resets[0]
    while r - w > start:
        r -= w
    out = []
    while r - w < end:
        out.append((r - w, r))
        r += w
    return out


def series(con: sqlite3.Connection, kind: str, start: int, end: int, step: int = BUCKET_MS) -> dict:
    est = estimate(con, kind)   # no estimate line until MIN_SAMPLES calibration samples (user decision)
    ratio = est["usd_per_pct"]
    if kind == "five_hour":
        windows = [(b.start_ms, b.end_ms, b.source) for b in compute_blocks(con, start - WINDOW_MS[kind])]
    else:
        windows = [(a, b, "statusline") for a, b in _week_windows(con, start, end)]
    usd = dict(con.execute("SELECT t5_ms, SUM(usd) FROM buckets WHERE t5_ms >= ? AND t5_ms < ? GROUP BY t5_ms",
                           (start - WINDOW_MS[kind], end)))
    obs = _measured_steps(con, kind)

    pts: list[Point] = []
    wi, oi, cum, cur_w, last_obs = 0, 0, 0.0, None, None
    t = start - start % step
    while t <= end:
        while wi < len(windows) and windows[wi][1] <= t:
            wi += 1
        w = windows[wi] if wi < len(windows) and windows[wi][0] <= t else None
        if w != cur_w:   # entering a new window: recompute cumulative usage from its start
            cur_w = w
            cum = sum(v for k, v in usd.items() if w and w[0] <= k < t) if w else 0.0
        while oi < len(obs) and obs[oi][0] <= t:
            last_obs = obs[oi]
            oi += 1
        meas = None
        if last_obs and t < last_obs[1]:              # still inside the observed window
            meas = last_obs[2]
        elif last_obs and t >= last_obs[1]:
            meas = 0.0 if t - last_obs[1] < WINDOW_MS[kind] else None
        e = min(100.0, cum / ratio) if (ratio and w) else (0.0 if ratio else None)
        pts.append(Point(t, meas, round(e, 2) if e is not None else None))
        cum += usd.get(t, 0.0) if w else 0.0
        t += step
    # the latest reading may fall between grid points: let it decide the value "now"
    later = [o for o in obs if pts and pts[-1].t < o[0] <= end and end < o[1]]
    if later:
        pts.append(Point(end, later[-1][2], pts[-1].estimated))
    return dict(kind=kind, points=pts, windows=windows, usd_per_pct=ratio, n_samples=est["n"],
                reliable=est["reliable"])


def pct_at(s: dict, t: int) -> tuple[float | None, str]:
    """Best value at t: measured if present, else estimated."""
    best = None
    for p in s["points"]:
        if p.t > t:
            break
        best = p
    if not best:
        return None, "none"
    if best.measured is not None:
        return best.measured, "measured"
    return best.estimated, "estimated" if best.estimated is not None else "none"
