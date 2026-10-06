import json

import pytest

from moni_token.blocks import compute_blocks, status
from moni_token.buckets import BUCKET_MS
from moni_token.collector import collect
from moni_token.settings import SpikeParams, load_spike_params
from moni_token.spikes import cache_write_spikes, detect, merge_runs, rate_spikes, suppress_realerts, Spike
from moni_token.units import call_usd, price_for, usd_parts

H = 3_600_000
M = 60_000
T0 = 1_791_244_800_000  # 2026-10-06T00:00:00Z


def iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------- units ----------

def test_prices_verified_and_estimated():
    assert price_for("claude-haiku-4-5-20251001").input == 1.0 and not price_for("claude-haiku-4-5-20251001").estimated
    assert price_for("claude-sonnet-5").output == 10.0
    assert price_for("claude-opus-5[1m]").input == 5.0
    assert price_for("claude-sonnet-9-9").estimated and price_for("claude-sonnet-9-9").input == 2.0
    assert price_for(None).estimated


def test_cache_write_1h_costs_twice_input_and_read_a_tenth():
    m = "claude-sonnet-5"  # $2 input
    assert call_usd(m, 0, 0, 0, 0, 1_000_000) == pytest.approx(4.0)
    assert call_usd(m, 0, 0, 0, 1_000_000, 0) == pytest.approx(2.5)
    assert call_usd(m, 0, 0, 1_000_000, 0, 0) == pytest.approx(0.2)
    assert call_usd(m, 0, 0, 0, 0, 0, web_search_n=10) == pytest.approx(0.1)
    parts = usd_parts(m, 1_000_000, 1_000_000, 0, 0, 0)
    assert parts["input"] == pytest.approx(2.0) and parts["output"] == pytest.approx(10.0)


# ---------- buckets via collector ----------

def test_buckets_sum_usd_per_5min_and_incremental(logs, con):
    p = logs.path()
    logs.write(p, logs.assistant("m1", iso(T0 + 1 * M), model="claude-sonnet-5", inp=1_000_000, cr=0, out=0),
               logs.assistant("m2", iso(T0 + 4 * M), model="claude-sonnet-5", inp=1_000_000, cr=0, out=0),
               logs.assistant("m3", iso(T0 + 6 * M), model="claude-sonnet-5", inp=0, cr=0, out=0, c1=1_000_000))
    collect(con, logs.root)
    rows = dict(con.execute("SELECT t5_ms, usd FROM buckets"))
    assert rows[T0] == pytest.approx(4.0) and rows[T0 + BUCKET_MS] == pytest.approx(4.0)
    logs.write(p, logs.assistant("m4", iso(T0 + 7 * M), model="claude-sonnet-5", inp=1_000_000, cr=0, out=0))
    collect(con, logs.root)
    rows = dict(con.execute("SELECT t5_ms, usd FROM buckets"))
    assert rows[T0] == pytest.approx(4.0) and rows[T0 + BUCKET_MS] == pytest.approx(6.0)


# ---------- rate spikes: boundary ----------

def put(con, t5, usd, sid="s"):
    con.execute("INSERT OR REPLACE INTO buckets VALUES (?,?,?,?,?,?,?,?,?,?,?)", (t5, "P", sid, 1, 0, 0, 0, 0, 0, usd, 0))


def flat_then_burst(con, base, burst):
    for i in range(12):                      # 60-min baseline: 1.0 per 5 min
        put(con, T0 + i * BUCKET_MS, base)
    for i in range(12, 15):                  # 15-min window
        put(con, T0 + i * BUCKET_MS, burst)
    return T0 + 15 * BUCKET_MS              # evaluation point = end of window


P = SpikeParams(floor_usd=0.5)


def test_rate_spike_fires_at_exactly_multiplier(con):
    t = flat_then_burst(con, 1.0, 3.0)       # window 9.0 vs baseline 3.0 -> x3.0
    hits = [s for s in rate_spikes(con, t, t, P)]
    assert len(hits) == 1 and hits[0].ratio == pytest.approx(3.0) and hits[0].usd == pytest.approx(9.0)
    assert hits[0].evidence["threshold_usd"] == pytest.approx(9.0)


def test_rate_spike_silent_just_below_multiplier(con):
    t = flat_then_burst(con, 1.0, 2.99)
    assert rate_spikes(con, t, t, P) == []


def test_floor_blocks_tiny_spikes_from_zero_baseline(con):
    t = flat_then_burst(con, 0.0, 0.1)       # baseline 0 -> any usage is "infinite x"; floor must stop it
    assert rate_spikes(con, t, t, P) == []
    t = flat_then_burst(con, 0.0, 0.2)       # 0.6 >= floor 0.5
    hits = rate_spikes(con, t, t, P)
    assert len(hits) == 1 and hits[0].ratio is None


def test_custom_multiplier_from_config(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("[spike]\nmultiplier = 5\nfloor_usd = 1.5\nbogus = 1\n", encoding="utf-8")
    monkeypatch.setenv("MONI_TOKEN_HOME", str(tmp_path))
    p = load_spike_params()
    assert p.multiplier == 5 and p.floor_usd == 1.5 and p.window_min == 15


def test_sliding_hits_merge_into_one_episode(con):
    flat_then_burst(con, 1.0, 5.0)
    hits = merge_runs(rate_spikes(con, T0 + 13 * BUCKET_MS, T0 + 20 * BUCKET_MS, P))
    assert len(hits) == 1 and hits[0].usd == pytest.approx(15.0)


def test_realert_suppressed_unless_escalated():
    p = SpikeParams(realert_min=30, escalate_factor=2.0)
    mk = lambda t, r: Spike("rate", t, t + 15 * M, "all", 1, 1, r)
    out = suppress_realerts([mk(0, 3.0), mk(10 * M, 4.0), mk(20 * M, 6.5), mk(61 * M, 3.0)], p)
    assert [s.start_ms for s in out] == [0, 20 * M, 61 * M]


# ---------- single-call cache rewrite (10/06 15:56 pattern) ----------

def test_cache_write_spike_reports_idle_gap(logs, con):
    p = logs.path()
    logs.write(p, logs.assistant("m1", iso(T0), cr=480_000),
               logs.assistant("m2", iso(T0 + 3 * H), cr=28_000, c1=458_116))
    collect(con, logs.root)
    hits = cache_write_spikes(con, T0, T0 + 4 * H, SpikeParams())
    assert len(hits) == 1
    e = hits[0].evidence
    assert e["cache_write_1h_tokens"] == 458_116 and e["idle_before_min"] == 180.0
    assert hits[0].usd == pytest.approx(call_usd("claude-opus-5", 2, 10, 28_000, 0, 458_116))


# ---------- 5-hour blocks ----------

def test_blocks_heuristic_floor_to_hour_and_no_overlap(con):
    put(con, T0 + 6 * H + 10 * M, 1.0)       # 06:10 -> block 06:00-11:00
    put(con, T0 + 11 * H + 5 * M, 1.0)       # 11:05 -> next block 11:00-16:00
    b = compute_blocks(con)
    assert [(x.start_ms - T0) // H for x in b] == [6, 11] and all(x.source == "estimated" for x in b)


def test_known_reset_overrides_heuristic(con):
    reset = T0 + 6 * H + 6 * M               # observed 15:06 KST reset = 06:06Z
    con.execute("INSERT INTO limit_obs VALUES (?,?,?,?,?,?)", (T0, "log", "five_hour", reset, "allowed", None))
    put(con, T0 + 3 * H, 2.0)
    put(con, T0 + 6 * H + 50 * M, 3.0)
    b = compute_blocks(con)
    assert b[0].start_ms == reset - 5 * H and b[0].end_ms == reset and b[0].source == "log"
    assert b[1].start_ms == reset and b[1].source == "estimated"   # next window starts at the known reset


def test_status_projection_linear(con):
    for i in range(3):
        put(con, T0 + i * BUCKET_MS, 1.0)
    now = T0 + 15 * M
    s = status(con, now)
    assert s["rate_usd_per_min"] == pytest.approx(3.0 / 15)
    assert s["block"]["projected_usd"] == pytest.approx(3.0 + 0.2 * (5 * 60 - 15))


def test_check_notifies_once_with_template(logs, con):
    from moni_token.alerts import check_and_notify
    p = logs.path()
    logs.write(p, logs.assistant("m1", iso(T0), cr=480_000), logs.assistant("m2", iso(T0 + 3 * H), c1=458_116))
    collect(con, logs.root)
    got = []
    notify = lambda title, body: got.append((title, body)) or True
    now = T0 + 3 * H + 2 * M
    check_and_notify(con, now, SpikeParams(floor_usd=100), notify)
    check_and_notify(con, now + 2 * M, SpikeParams(floor_usd=100), notify)
    assert len(got) == 1
    title, body = got[0]
    assert "캐시 재기록" in title and "458,116" in body and "180분" in body and "새 세션" in body


def test_stale_spikes_recorded_silently(logs, con):
    from moni_token.alerts import check_and_notify
    logs.write(logs.path(), logs.assistant("m1", iso(T0), cr=480_000), logs.assistant("m2", iso(T0 + 3 * H), c1=458_116))
    collect(con, logs.root)
    got = []
    check_and_notify(con, T0 + 4 * H, SpikeParams(floor_usd=100), lambda t, b: got.append(t) or True)
    assert got == [] and con.execute("SELECT delivered FROM alerts").fetchall() == [(0,)]


def test_schema_change_rebuilds_db(tmp_path):
    import sqlite3
    from moni_token.db import connect
    old = sqlite3.connect(tmp_path / "u.db")
    old.execute("CREATE TABLE calls (x)"); old.commit(); old.close()
    con = connect(tmp_path / "u.db")
    assert "project" in [r[1] for r in con.execute("PRAGMA table_info(calls)")]


def test_quota_limits_collected(logs, con):
    rec = logs.assistant("m1", iso(T0), model="<synthetic>")
    rec["quotaLimits"] = {"status": "rejected", "resetsAt": (T0 + 2 * H) // 1000, "rateLimitType": "five_hour"}
    logs.write(logs.path(), rec)
    collect(con, logs.root)
    assert con.execute("SELECT kind, resets_at_ms FROM limit_obs").fetchall() == [("five_hour", T0 + 2 * H)]
