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
            if any(mod == m or mod.startswith(m + ".") for m in FORBIDDEN_MODULES):
                bad.append(f"{f.name}: {mod}")
    assert not bad, bad


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


def test_all_commands_work_offline(no_network, logs, tmp_path, capsys):
    from moni_token.cli import main
    logs.write(logs.path(), logs.human("2026-10-06T03:31:00.000Z"),
               logs.assistant("m1", "2026-10-06T03:31:01.000Z", tools=["Read"]))
    db = tmp_path / "x.db"
    assert main(["--db", str(db), "--projects", str(logs.root), "collect"]) == 0
    assert main(["--db", str(db), "daily", "--json"]) == 0
    assert SECRET not in capsys.readouterr().out
