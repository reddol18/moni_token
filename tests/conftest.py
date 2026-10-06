import json
from pathlib import Path

import pytest

SECRET = "TOP-SECRET-BODY-7f3a"  # must never reach the database


class LogBuilder:
    """Writes synthetic Claude Code JSONL logs that mirror the real schema (M0 probe)."""

    def __init__(self, root: Path):
        self.root = root
        self.n = 0

    def path(self, project="E--proj", session="s1", agent=None, workflow=None) -> Path:
        p = self.root / project
        if agent:
            p = p / session / "subagents"
            if workflow:
                p = p / "workflows" / workflow
            p = p / f"agent-{agent}.jsonl"
        else:
            p = p / f"{session}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _uuid(self):
        self.n += 1
        return f"u{self.n}"

    def assistant(self, msg_id, ts, *, session="s1", out=10, inp=2, cr=1000, c5=0, c1=0, tools=(),
                  model="claude-opus-5", sidechain=False, agent=None, entrypoint="cli", stop=None, web=0):
        content = [{"type": "text", "text": SECRET}] + [
            {"type": "tool_use", "id": f"toolu_{self._uuid()}", "name": name, "input": {"command": SECRET}}
            for name in tools]
        return {"type": "assistant", "uuid": self._uuid(), "timestamp": ts, "sessionId": session,
                "isSidechain": sidechain, "agentId": agent, "entrypoint": entrypoint, "requestId": "req_" + msg_id,
                "message": {"id": msg_id, "model": model, "role": "assistant", "content": content, "stop_reason": stop,
                            "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                                      "cache_creation_input_tokens": c5 + c1,
                                      "cache_creation": {"ephemeral_5m_input_tokens": c5, "ephemeral_1h_input_tokens": c1},
                                      "server_tool_use": {"web_search_requests": web, "web_fetch_requests": 0}}}}

    def human(self, ts, session="s1", text=SECRET):
        return {"type": "user", "uuid": self._uuid(), "timestamp": ts, "sessionId": session,
                "message": {"role": "user", "content": text}}

    def tool_result(self, ts, payload: str, session="s1", image=False):
        content = [{"type": "text", "text": payload}]
        if image:
            content.append({"type": "image", "source": {"type": "base64", "data": "AAAA"}})
        return {"type": "user", "uuid": self._uuid(), "timestamp": ts, "sessionId": session,
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": content}]},
                "toolUseResult": {"stdout": SECRET}}

    def write(self, path: Path, *records, mode="a"):
        with open(path, mode, encoding="utf-8", newline="\n") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")


@pytest.fixture
def logs(tmp_path):
    return LogBuilder(tmp_path / "projects")


@pytest.fixture
def con(tmp_path):
    from moni_token.db import connect
    c = connect(tmp_path / "usage.db")
    yield c
    c.close()
