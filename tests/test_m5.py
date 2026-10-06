import io
import json
import sys

import pytest

from moni_token import statusline
from moni_token.blocks import compute_blocks
from moni_token.calibrate import add_manual, estimate, ingest_statusline
from moni_token.collector import collect
from moni_token.db import connect

H, M = 3_600_000, 60_000
T0 = 1_791_244_800_000

SL = {"session_id": "abc", "model": {"display_name": "Opus"}, "workspace": {"current_dir": "C:/secret/path"},
      "transcript_path": "C:/x.jsonl", "context_window": {"used_percentage": 41.5},
      "rate_limits": {"five_hour": {"used_percentage": 12.0, "resets_at": (T0 + 5 * H) // 1000},
                      "seven_day": {"used_percentage": 3.0, "resets_at": (T0 + 100 * H) // 1000}}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MONI_TOKEN_HOME", str(tmp_path))
    return tmp_path


def run_sl(monkeypatch, capsys, payload: bytes, argv=()):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))
    assert statusline.main(list(argv)) == 0
    return capsys.readouterr().out


def test_statusline_records_numbers_only_and_dedupes(home, monkeypatch, capsys):
    out = run_sl(monkeypatch, capsys, json.dumps(SL).encode())
    assert "5h 12%" in out and "ctx 42%" in out
    run_sl(monkeypatch, capsys, json.dumps(SL).encode())          # unchanged -> not appended
    lines = (home / "statusline.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert set(rec) == {"ts_ms", "sid", "fh_pct", "fh_reset", "sd_pct", "sd_reset", "ctx_pct"}
    assert "secret" not in lines[0] and "Opus" not in lines[0]


def test_statusline_never_fails(home, monkeypatch, capsys):
    assert run_sl(monkeypatch, capsys, b"not json").strip() == "moni_token"
    assert run_sl(monkeypatch, capsys, b"").strip()


def test_statusline_wraps_previous_command(home, monkeypatch, capsys):
    out = run_sl(monkeypatch, capsys, json.dumps(SL).encode(), ["--wrap", "echo previous-line"])
    assert "previous-line" in out and "5h" not in out
    assert (home / "statusline.jsonl").exists()


def test_statusline_feeds_window_boundary_and_calibration(home, logs, tmp_path, monkeypatch, capsys):
    con = connect(tmp_path / "u.db")
    reset = T0 + 6 * H + 6 * M
    sp = home / "statusline.jsonl"
    with open(sp, "w", encoding="utf-8") as f:
        for i, pct in enumerate([10, 10.5, 12, 15, 17, 20]):
            f.write(json.dumps(dict(ts_ms=T0 + 2 * H + i * 10 * M, sid="s", fh_pct=pct, fh_reset=reset // 1000,
                                    sd_pct=None, sd_reset=None, ctx_pct=None)) + "\n")
    logs.write(logs.path(), *[logs.assistant(f"m{i}", __import__("test_m2").iso(T0 + 2 * H + i * 5 * M + M),
                                             model="claude-sonnet-5", inp=500_000, cr=0, out=0) for i in range(12)])
    collect(con, logs.root)
    assert ingest_statusline(con, sp) == 6
    assert ingest_statusline(con, sp) == 0          # incremental
    b = [x for x in compute_blocks(con) if x.source == "statusline"]
    assert b and b[0].end_ms == reset
    e = estimate(con)
    assert e["n"] >= 3 and e["usd_per_pct"] > 0
    assert all(s["source"] == "statusline" for s in e["samples"])


def test_manual_calibration_survives_schema_rebuild(tmp_path):
    con = connect(tmp_path / "u.db")
    add_manual(con, T0, T0 + H, 5.0, T0)
    con.execute("PRAGMA user_version = 1"); con.commit(); con.close()
    con = connect(tmp_path / "u.db")
    assert con.execute("SELECT pct FROM calib_manual").fetchall() == [(5.0,)]


def test_usage_readings_survive_schema_rebuild_but_derived_obs_do_not(tmp_path):
    from moni_token.calibrate import add_usage_reading
    con = connect(tmp_path / "u.db")
    add_usage_reading(con, "five_hour", 49.0, T0 + 5 * H, T0)
    con.execute("INSERT INTO limit_obs VALUES (?,?,?,?,?,?)", (T0, "statusline", "five_hour", T0 + 5 * H, "", 10.0))
    con.execute("PRAGMA user_version = 1"); con.commit(); con.close()
    con = connect(tmp_path / "u.db")
    assert con.execute("SELECT source, used_pct FROM limit_obs").fetchall() == [("usage", 49.0)]


def test_check_logs_run_cost(home, logs, monkeypatch):
    from moni_token import alerts
    from moni_token.cli import main
    monkeypatch.setattr(alerts, "desktop_notify", lambda t, b: True)
    logs.write(logs.path(), logs.assistant("m1", "2026-10-06T03:31:01.000Z"))
    assert main(["--db", str(home / "u.db"), "--projects", str(logs.root), "check"]) == 0
    run = json.loads((home / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert run["wall_ms"] >= 0 and run["cpu_ms"] >= 0
    assert (home / "report.html").exists()
