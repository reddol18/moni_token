from moni_token import baseline
from moni_token.pctseries import Point

STEP = 300_000
H = 3_600_000


def _series(windows_values, gap=10):
    """Windows of len(values) steps, `gap` empty steps apart; returns a pctseries-like dict."""
    pts, wins, t = [], [], 0
    for vals in windows_values:
        a = t
        for v in vals:
            pts.append(Point(t, v, None))
            t += STEP
        wins.append((a, t, "log"))
        for _ in range(gap):
            pts.append(Point(t, 0.0, None))
            t += STEP
    return dict(points=pts, windows=wins)


def test_curves_align_by_offset_and_skip_empty_windows():
    s = _series([[1, 2, 3], [0, 0, 0], [2, 4, 6]])
    c = baseline.window_curves(s, before_ms=10**12)
    assert c == [{0: 1, 1: 2, 2: 3}, {0: 2, 1: 4, 2: 6}]        # the all-zero window says nothing
    assert baseline.window_curves(s, before_ms=s["windows"][1][1]) == [{0: 1, 1: 2, 2: 3}]   # only ended windows


def test_usual_quantiles_and_verdict_thresholds():
    curves = [{0: float(i)} for i in range(1, 7)]               # 1..6 at offset 0
    u = baseline.usual(curves, 0)
    assert (u["n"], u["p25"], u["p50"], u["p75"]) == (6, 2, 3, 5)
    assert baseline.verdict(6, u) == "high" and baseline.verdict(1, u) == "low" and baseline.verdict(3, u) == "normal"
    few = baseline.usual(curves[:4], 0)
    assert few == dict(n=4) and baseline.verdict(9, few) == "unknown"   # below MIN_WINDOWS: no verdict
    assert baseline.usual(curves, 7) is None


def test_pace_projects_the_recent_slope_and_warns_before_the_reset():
    a = 0
    pts = [Point(a + i * STEP, i * 1.0, None) for i in range(25)]      # +12%p per hour
    s = dict(points=pts, windows=[(a, a + 5 * H, "log")])
    now = pts[-1].t                                                     # 2h in, at 24%
    p = baseline.pace(s, "five_hour", now, 24.0, s["windows"][0])
    assert p["per_hour"] == 12.0 and p["at_reset"] == 60.0 and p["full_ms"] is None
    p = baseline.pace(s, "five_hour", now, 90.0, s["windows"][0])      # jumped: 66%p in the last hour
    assert p["full_ms"] is not None and now < p["full_ms"] < a + 5 * H
    assert baseline.pace(s, "five_hour", now, None, s["windows"][0]) is None


def test_baseline_on_the_report_data(con):
    from moni_token.report import build_data
    d = build_data(con, 1_790_000_000_000)
    for k in ("five_hour", "seven_day"):
        b = d[k]["base"]
        assert len(b["p50"]) == len(d[k]["m"]) == len(b["last_week"])   # aligned with the chart points
        assert b["verdict"] in ("high", "normal", "low", "unknown")
