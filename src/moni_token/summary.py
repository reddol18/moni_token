"""Template summaries of what could be saved: one sentence per event and a digest over a period.

Savings are shown in %p of the 5-hour limit: for a measured/estimated rise, rise x saving share of the
window (no price assumption needed); otherwise saving $ / calibrated $ per 1%; otherwise share of usage.
Overlapping alternatives are not added up — an event is summarised by its single largest alternative.
"""
from collections import defaultdict


def _pp(e: dict, c: dict, usd_per_pct: float | None) -> float | None:
    if e.get("rise"):
        return e["rise"] * (c.get("saving_share") or 0)
    if usd_per_pct:
        return (c.get("saving_usd") or 0) / usd_per_pct
    return None


def best_alternative(e: dict) -> dict | None:
    cands = [c for c in e["causes"] if (c.get("saving_usd") or 0) > 0]
    return max(cands, key=lambda c: c["saving_usd"]) if cands else None


def event_summary(e: dict, usd_per_pct: float | None) -> str:
    b = best_alternative(e)
    if not b:
        first = e["causes"][0] if e["causes"] else None
        if not first:
            return "규칙에 맞는 원인이 없어 절약 방안을 계산하지 않았다."
        return f"주원인은 {first['label']}. {first.get('alternative') or first['advice']}"
    pp = _pp(e, b, usd_per_pct)
    amount = (f"약 {pp:.1f}%p" + (f"(상승 +{e['rise']:.1f}%p의 {b['saving_share']:.0%})" if e.get("rise") else ""))\
        if pp is not None else f"구간 사용량의 약 {b['saving_share']:.0%}"
    return f"{amount}를 줄일 수 있었다 — {b['label']}: {b['alternative']} → {b['advice']}"


def digest(events: list[dict], usd_per_pct: float | None, week_usd_per_pct: float | None = None) -> dict:
    """Per cause over the period: how often it led an event, and the total of its best-alternative savings."""
    by = defaultdict(lambda: dict(events=0, pp=0.0, share_sum=0.0, label="", advice="", alternative=""))
    for e in events:
        b = best_alternative(e)
        if not b:
            continue
        d = by[b["label"]]
        d["events"] += 1
        d["pp"] += _pp(e, b, usd_per_pct) or 0.0
        d["share_sum"] += b["saving_share"]
        d["label"], d["advice"], d["alternative"] = b["label"], b["advice"], b["alternative"]
    rows = sorted(by.values(), key=lambda d: (-d["pp"], -d["events"]))
    total = sum(d["pp"] for d in rows)
    lines = [f"{d['label']}: 사건 {d['events']}건, 줄일 수 있었던 양 약 {d['pp']:.1f}%p → {d['advice']}" for d in rows]
    # 5-hour %p summed over many windows is hard to feel; the weekly limit is the common denominator
    week = total * usd_per_pct / week_usd_per_pct if (usd_per_pct and week_usd_per_pct) else None
    week_txt = f" — 주간 한도로 환산하면 약 {week:.1f}%" if week is not None else ""
    head = (f"최근 사건 {len(events)}건 중 {sum(d['events'] for d in rows)}건에서 절약 여지가 있었고, 합계 약 "
            f"{total:.1f}%p(5시간 한도 기준){week_txt}를 아낄 수 있었다." if rows else "절약 여지가 계산된 사건이 없다.")
    return dict(headline=head, items=rows, lines=lines, total_pp=total, total_week_pct=week)
