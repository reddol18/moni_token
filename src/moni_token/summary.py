"""Template summaries of what could be saved: one sentence per event and a digest over a period.

Savings are shown in %p of the 5-hour limit: for a measured/estimated rise, rise x saving share of the
window (no price assumption needed); otherwise saving $ / calibrated $ per 1%; otherwise share of usage.
Overlapping alternatives are not added up — an event is summarised by its single largest alternative.
"""
from collections import defaultdict

from .i18n import t


def _pp(e: dict, c: dict, usd_per_pct: float | None) -> float | None:
    if e.get("rise"):
        return e["rise"] * (c.get("saving_share") or 0)
    if usd_per_pct:
        return (c.get("saving_usd") or 0) / usd_per_pct
    return None


def best_alternative(e: dict) -> dict | None:
    cands = [c for c in e["causes"] if (c.get("saving_usd") or 0) > 0]
    return max(cands, key=lambda c: c["saving_usd"]) if cands else None


def event_summary(e: dict, usd_per_pct: float | None, lang: str = "ko") -> str:
    b = best_alternative(e)
    if not b:
        first = e["causes"][0] if e["causes"] else None
        if not first:
            return t("summary.no_cause", lang)
        return t("summary.main_cause", lang, label=first["label"], rest=first.get("alternative") or first["advice"])
    pp = _pp(e, b, usd_per_pct)
    if pp is None:
        amount = t("summary.amount_share", lang, share=b["saving_share"])
    elif e.get("rise"):
        amount = t("summary.amount_pp_rise", lang, pp=pp, rise=e["rise"], share=b["saving_share"])
    else:
        amount = t("summary.amount_pp", lang, pp=pp)
    return t("summary.event", lang, amount=amount, label=b["label"], alternative=b["alternative"], advice=b["advice"])


def digest(events: list[dict], usd_per_pct: float | None, week_usd_per_pct: float | None = None,
           lang: str = "ko") -> dict:
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
    lines = [t("digest.line", lang, label=d["label"], events=d["events"], pp=d["pp"], advice=d["advice"]) for d in rows]
    # 5-hour %p summed over many windows is hard to feel; the weekly limit is the common denominator
    week = total * usd_per_pct / week_usd_per_pct if (usd_per_pct and week_usd_per_pct) else None
    week_txt = t("digest.week", lang, week=week) if week is not None else ""
    head = (t("digest.head", lang, n=len(events), k=sum(d["events"] for d in rows), total=total, week=week_txt)
            if rows else t("digest.none", lang))
    return dict(headline=head, items=rows, lines=lines, total_pp=total, total_week_pct=week)
