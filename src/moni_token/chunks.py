"""How often raw chunks (not conclusions) are shipped to the API, per agent, and what carrying them costs.

A tool result enters the context at the call that consumes it. Its size in tokens is measured, not guessed:
the consuming call's context growth (context_k - context_{k-1} - output_{k-1}), split across that turn's
results by byte size. From then on every later call in the same context (same session and agent, until a
compaction) reads it again. A chunk's carried tokens = size x (1 + later calls).

"Total tokens" = input-side tokens processed plus output, over all calls of the agent in the period.
The cost-weighted share uses the bundled prices (cache write 1.25x/2x, cache read 0.1x).
"""
import bisect
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from .units import call_usd, price_for, table

CATEGORIES = [
    ("파일 읽기", lambda n: n in ("Read", "NotebookRead")),
    ("셸 출력", lambda n: n in ("Bash", "PowerShell", "BashOutput", "TaskOutput")),
    ("검색", lambda n: n in ("Grep", "Glob", "ToolSearch")),
    ("MCP(DB·외부 도구)", lambda n: n.startswith("mcp__")),
    ("웹", lambda n: n in ("WebFetch", "WebSearch")),
    ("서브에이전트 결과", lambda n: n in ("Task", "Agent", "Workflow")),
]


def category(name: str | None) -> str:
    if not name:
        return "기타(이름 없음)"
    return next((label for label, ok in CATEGORIES if ok(name)), f"기타({name})")


@dataclass
class AgentChunks:
    project: str
    calls: int = 0
    total_tokens: int = 0
    total_usd: float = 0.0
    chunks: int = 0
    chunk_sizes: list = field(default_factory=list)
    carried_tokens: int = 0
    carried_usd: float = 0.0
    by_category: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0, 0.0]))  # count, tokens, usd

    def as_dict(self) -> dict:
        cats = sorted(self.by_category.items(), key=lambda kv: -kv[1][2])
        return dict(
            project=self.project, calls=self.calls, chunks=self.chunks,
            per_100_calls=round(100 * self.chunks / self.calls, 1) if self.calls else 0.0,
            median_chunk=int(statistics.median(self.chunk_sizes)) if self.chunk_sizes else 0,
            max_chunk=max(self.chunk_sizes, default=0),
            token_share=round(self.carried_tokens / self.total_tokens, 4) if self.total_tokens else 0.0,
            cost_share=round(self.carried_usd / self.total_usd, 4) if self.total_usd else 0.0,
            carried_tokens=self.carried_tokens, total_tokens=self.total_tokens,
            categories=[dict(category=c, chunks=v[0], tokens=v[1],
                             cost_share=round(v[2] / self.total_usd, 4) if self.total_usd else 0.0) for c, v in cats])


def analyze_chunks(con: sqlite3.Connection, start_ms: int, end_ms: int, min_tokens: int = 5_000) -> dict:
    mul = table()["multipliers"]
    calls = con.execute(
        "SELECT msg_id, ts_ms, COALESCE(session_id,''), COALESCE(agent_id,''), COALESCE(project, project_dir), model, "
        "input, output, cache_read, cache_5m, cache_1h, web_search_n FROM calls WHERE ts_ms >= ? AND ts_ms < ? "
        "ORDER BY ts_ms", (start_ms, end_ms)).fetchall()
    results: dict[str, list] = defaultdict(list)
    for tid, mid, size, img, name in con.execute(
            "SELECT r.tool_use_id, r.msg_id, r.bytes, r.image, t.name FROM tool_results r "
            "LEFT JOIN call_tools t ON t.tool_use_id = r.tool_use_id "
            "JOIN calls c ON c.msg_id = r.msg_id WHERE c.ts_ms >= ? AND c.ts_ms < ?", (start_ms, end_ms)):
        results[mid].append((size, img, name))
    compacts: dict[tuple, list] = defaultdict(list)
    for sid, aid, ts in con.execute("SELECT COALESCE(session_id,''), COALESCE(agent_id,''), ts_ms FROM compactions "
                                    "ORDER BY ts_ms"):
        compacts[(sid, aid)].append(ts)

    contexts: dict[tuple, list] = defaultdict(list)
    agents: dict[str, AgentChunks] = {}
    for c in calls:
        mid, ts, sid, aid, proj, model, inp, out, cr, c5, c1, ws = c
        contexts[(sid, aid)].append(c)
        a = agents.setdefault(proj, AgentChunks(proj))
        a.calls += 1
        a.total_tokens += inp + cr + c5 + c1 + out
        a.total_usd += call_usd(model, inp, out, cr, c5, c1, ws)

    for key, rows in contexts.items():
        cuts = compacts.get(key, [])
        times = [r[1] for r in rows]
        for k, r in enumerate(rows):
            res = results.get(r[0])
            if not res or k == 0:
                continue
            prev = rows[k - 1]
            growth = (r[6] + r[8] + r[9] + r[10]) - (prev[6] + prev[8] + prev[9] + prev[10]) - prev[7]
            if growth <= 0:
                continue           # the context shrank (compaction) or was rebuilt: size unknown
            total_bytes = sum(s for s, _, _ in res) or 1
            nxt_cut = cuts[bisect.bisect_right(cuts, r[1])] if bisect.bisect_right(cuts, r[1]) < len(cuts) else None
            end_idx = bisect.bisect_left(times, nxt_cut) if nxt_cut else len(rows)
            later = max(0, end_idx - k - 1)
            pin = price_for(r[5]).input / 1e6
            wm = mul["cache_write_1h"] if r[10] >= r[9] else mul["cache_write_5m"]
            a = agents[r[4]]
            for size, img, name in res:
                tok = int(growth * size / total_bytes)
                if tok < min_tokens:
                    continue
                carried = tok * (1 + later)
                usd = tok * pin * wm + tok * later * pin * mul["cache_read"]
                a.chunks += 1
                a.chunk_sizes.append(tok)
                a.carried_tokens += carried
                a.carried_usd += usd
                cat = a.by_category[category(name)]
                cat[0] += 1
                cat[1] += carried
                cat[2] += usd
    rows = sorted((a.as_dict() for a in agents.values()), key=lambda d: -d["carried_tokens"])
    tot_t = sum(a.total_tokens for a in agents.values())
    tot_u = sum(a.total_usd for a in agents.values())
    overall = dict(calls=sum(a.calls for a in agents.values()), chunks=sum(a.chunks for a in agents.values()),
                   token_share=round(sum(a.carried_tokens for a in agents.values()) / tot_t, 4) if tot_t else 0.0,
                   cost_share=round(sum(a.carried_usd for a in agents.values()) / tot_u, 4) if tot_u else 0.0)
    return dict(start_ms=start_ms, end_ms=end_ms, min_tokens=min_tokens, agents=rows, overall=overall)
