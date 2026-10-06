"""Savings: counterfactual formulas and the template summaries."""
import re

import pytest

from moni_token.analysis import analyze
from moni_token.collector import collect
from moni_token.savings import SavingParams
from moni_token.summary import digest, event_summary
from moni_token.units import price_for

from test_m2 import iso

H, M = 3_600_000, 60_000
T0 = 1_791_244_800_000
SONNET = dict(model="claude-sonnet-5", out=0)
PIN = price_for("claude-sonnet-5").input / 1e6


def test_idle_resume_saving_counts_rewrite_and_later_reads(logs, con):
    p = logs.path(session="s")
    logs.write(p, logs.assistant("m0", iso(T0), session="s", cr=450_000, **SONNET),
               logs.assistant("m1", iso(T0 + 3 * H), session="s", cr=0, c1=430_000, **SONNET),
               *[logs.assistant(f"m{i}", iso(T0 + 3 * H + i * M), session="s", cr=430_000, **SONNET) for i in range(2, 6)])
    collect(con, logs.root)
    c = next(c for c in analyze(con, T0 + 2 * H, T0 + 4 * H).causes if c.code == "idle_resume")
    excess = 430_000 - SavingParams().fresh_ctx_tokens
    assert c.saving_usd == pytest.approx(excess * PIN * 2.0 + 4 * excess * PIN * 0.1, rel=1e-3)
    assert "새 세션" in c.alternative and "30k 가정" in c.alternative
    assert 0 < c.saving_share <= 1


def test_long_context_saving_is_net_of_recaching(logs, con):
    p = logs.path(session="s")
    logs.write(p, *[logs.assistant(f"m{i}", iso(T0 + i * M), session="s", cr=400_000, c1=1000, **SONNET) for i in range(25)])
    collect(con, logs.root)
    c = next(c for c in analyze(con, T0, T0 + H).causes if c.code == "long_context")
    t = SavingParams().compact_ctx_tokens
    assert c.saving_usd == pytest.approx(25 * (400_000 - t) * PIN * 0.1 - t * PIN * 2.0, rel=1e-3)
    assert "/compact" in c.alternative


def test_parallel_claims_no_saving_but_explains(logs, con):
    for s in ("a", "b", "c"):
        logs.write(logs.path(project=f"E--{s}", session=s),
                   *[logs.assistant(f"{s}{i}", iso(T0 + i * M), session=s, cr=1000, **SONNET) for i in range(3)])
    collect(con, logs.root)
    c = next(c for c in analyze(con, T0, T0 + H).causes if c.code == "parallel")
    assert c.saving_usd == 0 and "절약액은 없음" in c.alternative


def test_event_summary_uses_rise_share_and_best_alternative():
    e = dict(rise=4.0, causes=[
        dict(label="긴 대화 × 잦은 호출", advice="/compact", alternative="A", saving_usd=1.0, saving_share=0.25),
        dict(label="공백 후 재개(캐시 만료)", advice="새 세션", alternative="B", saving_usd=2.0, saving_share=0.5)])
    s = event_summary(e, usd_per_pct=None)
    assert s.startswith("약 2.0%p(상승 +4.0%p의 50%)") and "공백 후 재개" in s and "새 세션" in s


def test_event_summary_without_rise_uses_calibration_or_share():
    e = dict(rise=None, causes=[dict(label="L", advice="adv", alternative="alt", saving_usd=7.0, saving_share=0.3)])
    assert event_summary(e, usd_per_pct=3.5).startswith("약 2.0%p")
    assert event_summary(e, usd_per_pct=None).startswith("구간 사용량의 약 30%")


def test_digest_groups_by_best_cause():
    mk = lambda label, rise, share: dict(rise=rise, causes=[dict(label=label, advice="a", alternative="x",
                                                                 saving_usd=1.0, saving_share=share)])
    d = digest([mk("공백 후 재개(캐시 만료)", 4, 0.5), mk("공백 후 재개(캐시 만료)", 6, 0.5), mk("긴 대화 × 잦은 호출", 3, 0.2),
                dict(rise=3, causes=[])], None)
    assert d["items"][0]["label"] == "공백 후 재개(캐시 만료)" and d["items"][0]["events"] == 2
    assert d["total_pp"] == pytest.approx(2 + 3 + 0.6)
    assert "4건 중 3건" in d["headline"]
    assert not re.search(r"\{\w+", " ".join(d["lines"]))
