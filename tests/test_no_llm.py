"""Principle 0: the tool never calls an LLM and never touches the network (PLAN §2)."""
import ast
import socket
from pathlib import Path

import pytest

from conftest import SECRET

SRC = Path(__file__).resolve().parents[1] / "src" / "moni_token"
FORBIDDEN_MODULES = {"anthropic", "openai", "httpx", "requests", "aiohttp", "urllib3", "socket", "ssl",
                     "http.client", "urllib.request", "subprocess", "claude_agent_sdk", "litellm"}


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module


def test_no_llm_or_network_imports():
    bad = []
    for f in SRC.rglob("*.py"):
        for mod in _imports(ast.parse(f.read_text(encoding="utf-8"))):
            if mod == "subprocess" and f.name in ("notify.py", "statusline.py"):
                continue  # OS notifier / the user's own previous status line; both pinned by tests
            if any(mod == m or mod.startswith(m + ".") for m in FORBIDDEN_MODULES):
                bad.append(f"{f.name}: {mod}")
    assert not bad, bad


def test_notifier_runs_only_os_notifier():
    from moni_token import notify
    calls = []

    class R:
        returncode = 0

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["env"]["MONI_NOTIFY_BODY"]))
        return R()

    assert notify.desktop_notify("t", "body", run=fake_run)
    cmd, body = calls[0]
    assert cmd[0] in ("powershell", "osascript", "sh") and "claude" not in " ".join(cmd).lower().replace(
        "windowspowershell", "")
    assert body == "body" and "body" not in " ".join(cmd)   # text passed via env, not the command line


def test_no_claude_cli_invocation_strings():
    for f in SRC.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "claude -p" not in text and "api.anthropic.com" not in text, f.name


@pytest.fixture
def no_network(monkeypatch):
    def guard(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket, "socket", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)


def test_all_commands_work_offline(no_network, logs, tmp_path, capsys, monkeypatch):
    from moni_token import alerts
    from moni_token.cli import main
    monkeypatch.setattr(alerts, "desktop_notify", lambda t, b: True)
    monkeypatch.setenv("MONI_TOKEN_HOME", str(tmp_path))
    logs.write(logs.path(), logs.human("2026-10-06T03:31:00.000Z"),
               logs.assistant("m1", "2026-10-06T03:31:01.000Z", tools=["Read"], c1=300_000))
    db = tmp_path / "x.db"
    for cmd in (["collect"], ["daily", "--json"], ["status"], ["blocks"], ["spikes", "--all"], ["check"],
                ["suggest-floor"]):
        assert main(["--db", str(db), "--projects", str(logs.root), *cmd]) == 0, cmd
    out = capsys.readouterr().out
    assert SECRET not in out and "cache_write" in out
