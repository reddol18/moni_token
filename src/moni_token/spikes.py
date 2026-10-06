"""Spike detection. Deterministic: thresholds come from SpikeParams, evidence numbers travel with every hit."""
import sqlite3
import statistics
from dataclasses import dataclass, field

from .buckets import BUCKET_MS
from .settings import SpikeParams

MIN = 60_000


@dataclass
class Spike:
    kind: str                 # rate | cache_write
    start_ms: int
    end_ms: int
    scope: str                # "all" for rate, session id for cache_write
    usd: float
    baseline_usd: float       # same-length baseline (rate) / 0 for cache_write
    ratio: float | None
    evidence: dict = field(default_factory=dict)


def _series(con, start_ms, end_ms) -> dict[int, float]:
    return dict(con.execute("SELECT t5_ms, SUM(usd) FROM buckets WHERE t5_ms >= ? AND t5_ms < ? GROUP BY t5_ms",
                            (start_ms, end_ms)))


def rate_spikes(con: sqlite3.Connection, start_ms: int, end_ms: int, p: SpikeParams) -> list[Spike]:
    wn = p.window_min * MIN // BUCKET_MS
    bn = p.baseline_min * MIN // BUCKET_MS
    t0 = start_ms - start_ms % BUCKET_MS
    s = _series(con, t0 - (wn + bn) * BUCKET_MS, end_ms)
    out = []
    t = t0
    while t <= end_ms:                       # evaluate at each bucket boundary t: window = [t - wn, t)
        w = [s.get(t - (i + 1) * BUCKET_MS, 0.0) for i in range(wn)]
        win = sum(w)
        if win > 0:
            base_buckets = [s.get(t - (wn + i + 1) * BUCKET_MS, 0.0) for i in range(bn)]
            base = statistics.median(base_buckets) * wn
            threshold = max(p.multiplier * base, p.floor_usd)
            if win >= threshold:
                out.append(Spike("rate", t - wn * BUCKET_MS, t, "all", win, base,
                                 win / base if base > 0 else None,
                                 dict(threshold_usd=threshold, multiplier=p.multiplier, floor_usd=p.floor_usd,
                                      window_min=p.window_min, baseline_min=p.baseline_min)))
        t += BUCKET_MS
    return out


def cache_write_spikes(con: sqlite3.Connection, start_ms: int, end_ms: int, p: SpikeParams) -> list[Spike]:
    from .units import call_usd
    out = []
    for (ts, sid, proj, name, model, inp, outp, cr, c5, c1) in con.execute(
            "SELECT ts_ms, session_id, project_dir, project, model, input, output, cache_read, cache_5m, cache_1h "
            "FROM calls "
            "WHERE ts_ms >= ? AND ts_ms < ? AND cache_5m + cache_1h >= ? ORDER BY ts_ms",
            (start_ms, end_ms, p.single_call_cache_write_tokens)):
        gap = con.execute("SELECT MAX(ts_ms) FROM calls WHERE session_id = ? AND ts_ms < ?", (sid, ts)).fetchone()[0]
        out.append(Spike("cache_write", ts, ts + 1, sid or "", call_usd(model, inp, outp, cr, c5, c1), 0.0, None,
                         dict(project_dir=proj, project=name or proj, cache_write_tokens=c5 + c1, cache_write_1h_tokens=c1,
                              idle_before_min=None if gap is None else round((ts - gap) / MIN, 1),
                              threshold_tokens=p.single_call_cache_write_tokens)))
    return out


def merge_runs(spikes: list[Spike]) -> list[Spike]:
    """Consecutive rate hits (sliding windows) become one episode; keep the strongest window's numbers."""
    out: list[Spike] = []
    for s in spikes:
        prev = out[-1] if out else None
        if prev and s.kind == prev.kind == "rate" and s.start_ms <= prev.end_ms:
            peak = s if s.usd > prev.usd else prev
            out[-1] = Spike("rate", prev.start_ms, s.end_ms, "all", peak.usd, peak.baseline_usd, peak.ratio,
                            {**peak.evidence, "peak_window_start_ms": peak.start_ms})
        else:
            out.append(s)
    return out


def suppress_realerts(spikes: list[Spike], p: SpikeParams) -> list[Spike]:
    """Alert policy: one alert per scope per realert_min unless the ratio escalated."""
    last: dict[tuple, Spike] = {}
    out = []
    for s in sorted(spikes, key=lambda x: x.start_ms):
        key = (s.kind, s.scope)
        prev = last.get(key)
        if prev and s.start_ms - prev.start_ms < p.realert_min * MIN:
            grew = s.ratio is not None and prev.ratio is not None and s.ratio >= p.escalate_factor * prev.ratio
            if not grew:
                continue
        last[key] = s
        out.append(s)
    return out


def pct_spikes(con: sqlite3.Connection, start_ms: int, end_ms: int, p: SpikeParams) -> list[Spike] | None:
    """Sharp rises of the 5-hour limit % (measured, else estimated). None when no % is available at all."""
    from .pctseries import series
    s = series(con, "five_hour", start_ms - p.window_min * MIN, end_ms)
    vals = {pt.t: (pt.measured, "measured") if pt.measured is not None else (pt.estimated, "estimated")
            for pt in s["points"]}
    if all(v is None for v, _ in vals.values()):
        return None
    win_of = lambda t: next((w for w in s["windows"] if w[0] <= t < w[1]), None)
    usd = dict(_series(con, start_ms - p.window_min * MIN, end_ms))
    out = []
    span = p.window_min * MIN
    for t, (v, basis) in sorted(vals.items()):
        if t < start_ms or v is None:
            continue
        v0, _ = vals.get(t - span, (None, ""))
        if win_of(t) != win_of(t - span):   # window reset inside the span: the rise starts from 0
            v0 = 0.0
        if v0 is None:
            continue
        rise = v - v0
        if rise >= p.pct_jump:
            out.append(Spike("pct", t - span, t, "all", sum(u for k, u in usd.items() if t - span <= k < t), 0.0, None,
                             dict(from_pct=round(v0, 1), to_pct=round(v, 1), rise_pp=round(rise, 1), basis=basis,
                                  window_min=p.window_min, threshold_pp=p.pct_jump,
                                  usd_per_pct=s["usd_per_pct"], calib_samples=s["n_samples"])))
    return out


def merge_pct_runs(spikes: list[Spike]) -> list[Spike]:
    out: list[Spike] = []
    for s in spikes:
        prev = out[-1] if out else None
        if prev and s.start_ms <= prev.end_ms:
            peak = s if s.evidence["rise_pp"] > prev.evidence["rise_pp"] else prev
            ev = {**peak.evidence, "from_pct": prev.evidence["from_pct"], "to_pct": s.evidence["to_pct"]}
            ev["rise_pp"] = round(ev["to_pct"] - ev["from_pct"], 1) if ev["to_pct"] >= ev["from_pct"] else peak.evidence["rise_pp"]
            out[-1] = Spike("pct", prev.start_ms, s.end_ms, "all", prev.usd + s.usd, 0.0, None, ev)
        else:
            out.append(s)
    return out


def detect(con: sqlite3.Connection, start_ms: int, end_ms: int, p: SpikeParams) -> list[Spike]:
    pct = pct_spikes(con, start_ms, end_ms, p)
    main = merge_pct_runs(pct) if pct is not None else merge_runs(rate_spikes(con, start_ms, end_ms, p))
    return suppress_realerts(main + cache_write_spikes(con, start_ms, end_ms, p), p)


def suggest_floor(con: sqlite3.Connection, end_ms: int, days: int = 7, window_min: int = 15, q: float = 0.75) -> float | None:
    """p75 of active (non-zero) window totals over the past `days` — a data-driven floor_usd candidate."""
    wn = window_min * MIN // BUCKET_MS
    s = _series(con, end_ms - days * 86_400_000, end_ms)
    if not s:
        return None
    t, stop, vals = min(s), max(s) + BUCKET_MS, []
    while t <= stop:
        v = sum(s.get(t - (i + 1) * BUCKET_MS, 0.0) for i in range(wn))
        if v > 0:
            vals.append(v)
        t += BUCKET_MS
    vals.sort()
    return vals[min(len(vals) - 1, int(len(vals) * q))]
