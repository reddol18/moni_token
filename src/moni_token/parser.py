"""Turn Claude Code JSONL lines into numeric call records.

Only counts, sizes, ids and names leave this module; message text is read (to measure size) and dropped.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime


@dataclass
class FileState:
    """What happened in this file since the last API call — attributed to the next call."""
    trigger: str | None = None
    result_bytes: int = 0
    result_image: bool = False
    attach_bytes: int = 0

    def reset(self) -> None:
        self.trigger, self.result_bytes, self.result_image, self.attach_bytes = None, 0, False, 0


def ts_to_ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def _content_bytes(content) -> int:
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    return len(json.dumps(content, ensure_ascii=False).encode("utf-8"))


def _has_image(content) -> bool:
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in content)


def _observe_user(d: dict, state: FileState) -> None:
    msg = d.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results:
            for b in results:
                state.result_bytes += _content_bytes(b.get("content", ""))
                state.result_image = state.result_image or _has_image(b.get("content"))
            if state.trigger != "human":
                state.trigger = "tool_result"
            return
        if _has_image(content):
            state.result_image = True
    if d.get("isMeta") or d.get("isCompactSummary"):
        state.trigger = state.trigger or "other"
    else:
        state.trigger = "human"


def call_record(d: dict, project_dir: str, state: FileState) -> tuple[dict, list[tuple[str, str]]] | None:
    """Build a call row from an assistant line. Returns (row, [(tool_use_id, name)]) or None."""
    msg = d.get("message") or {}
    usage = msg.get("usage")
    model = msg.get("model")
    if not usage or model == "<synthetic>":
        return None
    cc = usage.get("cache_creation") or {}
    stu = usage.get("server_tool_use") or {}
    otd = usage.get("output_tokens_details") or {}
    c5, c1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    if c5 is None and c1 is None:  # older logs: no TTL split, treat as 5m (the default TTL)
        c5, c1 = usage.get("cache_creation_input_tokens", 0), 0
    ts = d["timestamp"]
    row = dict(
        msg_id=msg.get("id") or d.get("requestId") or d["uuid"],
        ts=ts, ts_ms=ts_to_ms(ts),
        session_id=d.get("sessionId"), project_dir=project_dir,
        project=re.split(r"[\\/]", d["cwd"].rstrip("\\/"))[-1] if d.get("cwd") else None,
        agent_id=d.get("agentId"),
        is_sidechain=int(bool(d.get("isSidechain"))), entrypoint=d.get("entrypoint"), model=model,
        input=usage.get("input_tokens", 0) or 0, output=usage.get("output_tokens", 0) or 0,
        cache_read=usage.get("cache_read_input_tokens", 0) or 0, cache_5m=c5 or 0, cache_1h=c1 or 0,
        thinking=otd.get("thinking_tokens", 0) or 0,
        web_search_n=stu.get("web_search_requests", 0) or 0, web_fetch_n=stu.get("web_fetch_requests", 0) or 0,
        stop_reason=msg.get("stop_reason"),
        trigger=state.trigger or "other",
        prev_result_bytes=state.result_bytes, prev_result_image=int(state.result_image),
        prev_attach_bytes=state.attach_bytes,
    )
    tools = [(b.get("id") or f"{i}", b.get("name") or "?") for i, b in enumerate(msg.get("content") or [])
             if isinstance(b, dict) and b.get("type") == "tool_use"]
    return row, tools


def quota_observation(d: dict) -> dict | None:
    """`quotaLimits` (rare, on rate-limit responses) carries the real window reset time."""
    q = d.get("quotaLimits")
    if not isinstance(q, dict) or not isinstance(q.get("resetsAt"), (int, float)) or not d.get("timestamp"):
        return None
    r = q["resetsAt"]
    return dict(ts_ms=ts_to_ms(d["timestamp"]), source="log",
                kind=str(q.get("rateLimitType") or "unknown"),
                resets_at_ms=int(r * 1000 if r < 1e12 else r), status=str(q.get("status") or ""))


def process_line(raw: bytes, project_dir: str, state: FileState, last_msg_id: list,
                 quota_sink: list | None = None) -> tuple[dict, list] | None:
    """Feed one raw line. Returns a call record for assistant lines, else updates state.

    `last_msg_id` is a 1-element list: continuation lines of the same response must not consume the
    pending state that belongs to the response as a whole.
    """
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    t = d.get("type")
    if quota_sink is not None and "quotaLimits" in d:
        q = quota_observation(d)
        if q:
            quota_sink.append(q)
    if t == "user":
        _observe_user(d, state)
        return None
    if t == "attachment":
        state.attach_bytes += len(raw)
        return None
    if t != "assistant":
        return None
    rec = call_record(d, project_dir, state)
    if rec is None:
        return None
    if rec[0]["msg_id"] != last_msg_id[0]:
        last_msg_id[0] = rec[0]["msg_id"]
        state.reset()
    return rec
