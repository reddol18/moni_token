"""Live agent map (issue #3, ADR-0004): where each agent is working in its project.

Three views of one project, all deterministic:
- tree     : the real directory tree under the session's cwd, with the paths agents touched
- pages    : UI routes found from real files (Next.js app/ and pages/, static *.html)
- features : headings of a contract document (PLAN/SPEC/task md), linked to files only through paths and file
             names written in that document; checklist items give the done count

From the logs only tool names and project-relative target paths are used. The directory tree, page list and
contract headings are read from the project folder at request time and are not stored.
"""
import json
import ntpath
import os
import posixpath
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from .blocks import compute_blocks
from .calibrate import estimate
from .chunks import measure_results
from .forecast import forecast_agent, forecast_checklist
from .parser import rel_path
from .units import call_usd

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build", ".next",
             ".turbo", ".cache", "coverage", ".idea", ".vscode", ".mypy_cache", ".ruff_cache", "target"}
MAX_ENTRIES = 4000
PER_DIR = 200
RUNNING_MS = 2 * 60_000
DESC_MAX = 80
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CONTRACT_GLOBS = ("docs/PLAN.md", "PLAN.md", "SPEC.md", "docs/SPEC.md", "docs/tasks/*.md")
PAGE_EXT = (".tsx", ".jsx", ".ts", ".js", ".mdx")


KINDS = (("edit", EDIT_TOOLS), ("read", {"Read", "Grep", "Glob", "NotebookRead", "LS"}),
         ("run", {"Bash", "PowerShell", "BashOutput", "TaskOutput", "KillShell"}), ("web", {"WebFetch", "WebSearch"}),
         ("agent", {"Agent", "Task", "Workflow", "SendMessage"}))


def tool_kind(name: str | None) -> str:
    return next((k for k, names in KINDS if name in names), "mcp" if (name or "").startswith("mcp__") else "other")


def _join(root: str, rel: str) -> str:
    mod = ntpath if (":" in root[:3] or "\\" in root) else posixpath
    return mod.join(root, rel)


def active_projects(con: sqlite3.Connection, since_ms: int, until_ms: int) -> list[dict]:
    rows = con.execute("SELECT project_dir, project, MAX(ts_ms), COUNT(*) FROM calls WHERE ts_ms >= ? AND ts_ms <= ? "
                       "GROUP BY project_dir ORDER BY MAX(ts_ms) DESC", (since_ms, until_ms)).fetchall()
    return [dict(project_dir=d, name=n or d, last_ms=t, calls=c, root=project_root(con, d)) for d, n, t, c in rows]


def project_root(con: sqlite3.Connection, project_dir: str) -> str | None:
    """The cwd that main sessions of this project used most recently (subagents can run in subfolders)."""
    r = con.execute("SELECT cwd FROM calls WHERE project_dir=? AND cwd IS NOT NULL "
                    "ORDER BY (agent_id IS NULL) DESC, ts_ms DESC LIMIT 1", (project_dir,)).fetchone()
    return r[0] if r else None


def _meta(projects: Path, project_dir: str, session_id: str) -> dict[str, dict]:
    """agentType / finished / parent / description from each subagent's .meta.json.

    The description (the short task title the parent gave) is shown on the live page only and never stored
    (ADR-0004 addendum); it is cut to DESC_MAX characters."""
    out = {}
    base = projects / project_dir / (session_id or "") / "subagents"
    if session_id and base.is_dir():
        for m in base.rglob("agent-*.meta.json"):
            try:
                d = json.loads(m.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            aid = m.name[len("agent-"):-len(".meta.json")]
            desc = d.get("description")
            out[aid] = dict(type=str(d.get("agentType") or ""), finished=bool(d.get("finished")),
                            parent=d.get("parentAgentId"), depth=d.get("spawnDepth"),
                            desc=str(desc)[:DESC_MAX] if isinstance(desc, str) else None)
    return out


def agents(con: sqlite3.Connection, projects: Path, project_dir: str, root: str, since_ms: int, now_ms: int,
           live: bool = True) -> list[dict]:
    per: dict[tuple, dict] = {}
    for sid, aid, ts, cwd, model, inp, out, cr, c5, c1, web in con.execute(
            "SELECT session_id, agent_id, ts_ms, cwd, model, input, output, cache_read, cache_5m, cache_1h, "
            "web_search_n FROM calls WHERE project_dir=? AND ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms",
            (project_dir, since_ms, now_ms)):
        a = per.setdefault((sid, aid), dict(session=(sid or "")[:8], session_id=sid, agent=aid, first_ms=ts, calls=0,
                                            tokens=0, usd=0.0))
        a["last_ms"] = ts
        a["calls"] += 1
        a["tokens"] += inp + out + cr + c5 + c1
        a["usd"] += call_usd(model, inp, out, cr, c5, c1, web)
    metas: dict[str, dict] = {}
    for sid in {k[0] for k in per}:
        metas.update(_meta(projects, project_dir, sid))
    for (sid, aid), a in per.items():
        m = metas.get(aid or "", {})
        a["type"] = "main" if aid is None else (m.get("type") or "subagent")
        a["parent"] = m.get("parent")
        a["desc"] = m.get("desc")
        recent = now_ms - a["last_ms"] < RUNNING_MS
        if live:      # the meta file says it finished: trust it right away
            a["status"] = "done" if m.get("finished") else ("running" if recent else "idle")
        else:         # a past moment (?at=) must not use today's "finished" flag
            a["status"] = "running" if recent else ("done" if m.get("finished") else "idle")
        a["touches"], a["tool"], a["where"], a["lanes"] = [], None, None, []
    for sid, aid, ts, cwd, name, path in con.execute(
            "SELECT c.session_id, c.agent_id, c.ts_ms, c.cwd, t.name, t.path FROM call_tools t JOIN calls c "
            "USING(msg_id) WHERE c.project_dir=? AND c.ts_ms >= ? AND c.ts_ms <= ? ORDER BY c.ts_ms",
            (project_dir, since_ms, now_ms)):
        a = per.get((sid, aid))
        if not a:
            continue
        a["tool"] = name
        p = None
        if path is not None:
            p = path if (not cwd or cwd == root) else rel_path(_join(cwd, path), root)   # subfolder cwd -> project root
        a["lanes"].append([ts, tool_kind(name), p])   # swimlane segment (issue #5)
        if p is None:
            continue
        a["touches"].append(dict(t=ts, path=p, edit=name in EDIT_TOOLS, tool=name))
        a["where"] = p
    out = sorted(per.values(), key=lambda a: (a["status"] != "running", -a["last_ms"]))
    for i, a in enumerate(out):
        a["key"] = f"a{i}"
        a["usd"] = round(a["usd"], 3)
    return out


def tree(root: Path, must: set[str] = frozenset()) -> list[dict]:
    """Directory entries (names only) under root.

    Each folder lists at most PER_DIR entries (the rest becomes one `more` entry), the whole tree at most MAX_ENTRIES,
    so one huge output folder cannot crowd out the rest. Paths in `must` (what agents touched) are always included.
    """
    out: list[dict] = []
    if not root.is_dir():
        return out
    seen: set[str] = set()

    def add(path: str, is_dir: bool) -> None:
        if path not in seen:
            seen.add(path)
            out.append(dict(path=path, dir=is_dir))

    queue = [""]   # breadth-first: every top-level folder shows before any deep one
    while queue and len(out) < MAX_ENTRIES:
        rel = queue.pop(0)
        try:
            items = sorted(os.scandir(root / rel), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            continue
        shown = 0
        for e in items:
            is_dir = e.is_dir(follow_symlinks=False)
            if is_dir and (e.name in SKIP_DIRS or (e.name.startswith(".") and e.name != ".github")
                           or Path(e.path, ".git").exists()):   # a nested repository is another project
                continue
            p = f"{rel}/{e.name}" if rel else e.name
            if shown >= PER_DIR or len(out) >= MAX_ENTRIES:
                out.append(dict(path=f"{rel}/…" if rel else "…", dir=False, more=len(items) - shown))
                break
            add(p, is_dir)
            shown += 1
            if is_dir:
                queue.append(p)
    for p in sorted(must):   # touched paths and their folders, even past the limits
        parts = p.split("/")
        for i in range(1, len(parts)):
            add("/".join(parts[:i]), True)
        if p:
            add(p, (root / p).is_dir())
    return out


def pages(entries: list[dict]) -> list[dict]:
    """UI routes from real files. `dir` is the folder whose files belong to the page (None: only the file)."""
    out = []
    for e in entries:
        p = e["path"]
        if e["dir"]:
            continue
        parts = p.split("/")
        name = parts[-1]
        stem, ext = posixpath.splitext(name)
        if "app" in parts and stem == "page" and ext in PAGE_EXT:        # Next.js app router
            i = len(parts) - 1 - parts[::-1].index("app")
            segs = [s for s in parts[i + 1:-1] if not (s.startswith("(") and s.endswith(")"))]
            out.append(dict(route="/" + "/".join(segs), file=p, dir="/".join(parts[:-1]), kind="next-app"))
        elif "pages" in parts and ext in PAGE_EXT and not stem.startswith("_"):   # Next.js pages router
            i = len(parts) - 1 - parts[::-1].index("pages")
            segs = parts[i + 1:-1] + ([] if stem == "index" else [stem])
            if segs[:1] == ["api"]:
                continue
            out.append(dict(route="/" + "/".join(segs), file=p, dir=None, kind="next-pages"))
        elif ext == ".html":
            out.append(dict(route=p, file=p, dir=None, kind="html"))
    return sorted(out, key=lambda x: x["route"])


def page_of(path: str, pgs: list[dict]) -> dict | None:
    best = None
    for pg in pgs:
        if path == pg["file"]:
            return pg
        d = pg["dir"]
        if d is not None and (path == d or path.startswith(d + "/")) and (best is None or len(d) > len(best["dir"])):
            best = pg
    return best


TICK = re.compile(r"`([^`\n]+)`")
CHECK = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+")
HEAD = re.compile(r"^(#{2,3})\s+(.+?)\s*#*\s*$")


def features(root: Path, entries: list[dict]) -> list[dict]:
    """Sections of the contract documents and the real paths each section names."""
    paths = {e["path"] for e in entries}
    by_name = defaultdict(list)
    for e in entries:
        by_name[posixpath.basename(e["path"])].append(e["path"])
    docs = []
    for g in CONTRACT_GLOBS:
        docs += sorted(root.glob(g))
    out = []
    for doc in docs:
        try:
            lines = doc.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        cur = None
        for ln in lines:
            h = HEAD.match(ln)
            if h:
                cur = dict(doc=doc.relative_to(root).as_posix(), title=h.group(2), level=len(h.group(1)),
                           paths=set(), done=0, total=0)
                out.append(cur)
                continue
            if cur is None:
                continue
            c = CHECK.match(ln)
            if c:
                cur["total"] += 1
                cur["done"] += c.group(1) in "xX"
            for tok in TICK.findall(ln):
                tok = tok.strip().strip("/").removeprefix("./")
                if tok in paths:
                    cur["paths"].add(tok)
                elif "/" not in tok and "." in tok and len(by_name.get(tok, [])) == 1:   # unique file name
                    cur["paths"].add(by_name[tok][0])
    return [dict(f, paths=sorted(f["paths"])) for f in out if f["paths"] or f["total"]]


def features_of(path: str, feats: list[dict]) -> list[int]:
    return [i for i, f in enumerate(feats) if any(path == p or path.startswith(p + "/") for p in f["paths"])]


def build_state(con: sqlite3.Connection, projects: Path, now_ms: int, project_dir: str | None = None,
                window_min: int = 30, live: bool = True) -> dict:
    """`live` is False when looking at a past moment (?at=)."""
    since = now_ms - window_min * 60_000
    projs = active_projects(con, since, now_ms)
    for p in projs:   # default: the project with subagents active right now, then the most recent one
        p["subagents_now"] = con.execute(
            "SELECT COUNT(DISTINCT agent_id) FROM calls WHERE project_dir=? AND agent_id IS NOT NULL AND ts_ms >= ? "
            "AND ts_ms <= ?", (p["project_dir"], now_ms - RUNNING_MS, now_ms)).fetchone()[0]
    first = max(projs, key=lambda p: (p["subagents_now"] > 0, p["last_ms"])) if projs else None
    # ?project= takes the log folder name or the display name (spike links from the usage page carry the name)
    cur = next((p for p in projs if p["project_dir"] == project_dir), None) or \
        next((p for p in projs if p["name"] == project_dir), first)
    state = dict(now=now_ms, window_min=window_min, projects=projs, project=cur, agents=[], tree=[], pages=[],
                 features=[], window=None, costs={}, docs=[])
    if not cur:
        return state
    root = cur["root"]
    ags = agents(con, projects, cur["project_dir"], root, since, now_ms, live) if root else []
    entries = tree(Path(root), {t["path"] for a in ags for t in a["touches"]}) if root else []
    pgs = pages(entries)
    feats = features(Path(root), entries) if root else []
    for a in ags:
        for t in a["touches"]:
            pg = page_of(t["path"], pgs)
            t["page"] = pg["route"] if pg else None
            t["features"] = features_of(t["path"], feats)
        a["touches"] = a["touches"][-200:]
        a["lanes"] = a["lanes"][-400:]
    costs = file_costs(con, cur["project_dir"], root, since, now_ms, ags)
    for a in ags:   # issue #7: how far along each unfinished subagent probably is
        a["forecast"] = None
        if a["agent"] and a["status"] != "done":
            n, first, usd = run_so_far(con, a["session_id"], a["agent"], now_ms)
            a["forecast"] = forecast_agent(con, a["type"], cur["project_dir"], n, now_ms - first, usd)
    docs = sorted({f["doc"] for f in feats})
    state["docs"] = [dict(doc=d, **forecast_checklist(con, root, d, now_ms)) for d in docs] if root else []
    state.update(agents=ags, tree=entries, pages=pgs, features=feats, costs=costs,
                 window=window_shares(con, now_ms, projs, ags, cur["project_dir"]))
    return state


def run_so_far(con: sqlite3.Connection, session_id: str, agent_id: str, now_ms: int) -> tuple[int, int, float]:
    """Calls, first call time and usage of one agent run up to now (not only inside the map's window)."""
    n, first, usd = 0, now_ms, 0.0
    for ts, model, inp, out, cr, c5, c1, web in con.execute(
            "SELECT ts_ms, model, input, output, cache_read, cache_5m, cache_1h, web_search_n FROM calls "
            "WHERE session_id IS ? AND agent_id = ? AND ts_ms <= ?", (session_id, agent_id, now_ms)):
        n, first, usd = n + 1, min(first, ts), usd + call_usd(model, inp, out, cr, c5, c1, web)
    return n, first, usd


def file_costs(con: sqlite3.Connection, project_dir: str, root: str, since_ms: int, now_ms: int,
               ags: list[dict]) -> dict[str, dict]:
    """Issue #6: per touched file, the tokens its tool results added and how often later calls re-read them.

    Same measurement as `moni-token chunks` (chunks.measure_results); ≈%p uses the 5-hour calibration."""
    paths = {}
    for tid, cwd, path in con.execute(
            "SELECT t.tool_use_id, c.cwd, t.path FROM call_tools t JOIN calls c USING(msg_id) "
            "WHERE c.project_dir=? AND c.ts_ms >= ? AND c.ts_ms <= ? AND t.path IS NOT NULL",
            (project_dir, since_ms, now_ms)):
        p = path if (not cwd or cwd == root) else rel_path(_join(cwd, path), root)
        if p:
            paths[tid] = p
    ratio = estimate(con, "five_hour")["usd_per_pct"]
    out: dict[str, dict] = {}
    per_agent: dict[tuple, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for m in measure_results(con, since_ms, now_ms + 1, project_dir):
        p = paths.get(m.tool_use_id)
        if not p or m.tokens <= 0:
            continue
        c = out.setdefault(p, dict(tokens=0, carried=0, reads=0, later=0, usd=0.0))
        c["tokens"] += m.tokens
        c["carried"] += m.carried
        c["reads"] += 1
        c["later"] = max(c["later"], m.later)
        c["usd"] += m.usd
        per_agent[(m.session_id, m.agent_id)][p] += m.carried
    for c in out.values():
        c["pp"] = round(c.pop("usd") / ratio, 2) if ratio else None
    for a in ags:
        files = per_agent.get((a["session_id"] or "", a["agent"] or ""), {})
        top = max(files.items(), key=lambda kv: kv[1], default=None)
        a["top_file"] = dict(path=top[0], carried=top[1]) if top else None
    return out


def window_shares(con: sqlite3.Connection, now_ms: int, projs: list[dict], ags: list[dict], project_dir: str) -> dict | None:
    """Usage so far in the 5-hour window containing now_ms, per project and per agent (issue #4).

    Shares are of this PC's local log usage; ≈%p uses the 5-hour calibration and is an estimate."""
    blk = next((b for b in compute_blocks(con, now_ms - 6 * 3_600_000) if b.start_ms <= now_ms < b.end_ms), None)
    if not blk:
        for a in ags:
            a["win"] = None
        return None
    ratio = estimate(con, "five_hour")["usd_per_pct"]
    per_proj: dict[str, float] = defaultdict(float)
    per_agent: dict[tuple, float] = defaultdict(float)
    for pd, sid, aid, model, inp, out, cr, c5, c1, web in con.execute(
            "SELECT project_dir, session_id, agent_id, model, input, output, cache_read, cache_5m, cache_1h, "
            "web_search_n FROM calls WHERE ts_ms >= ? AND ts_ms <= ?", (blk.start_ms, now_ms)):
        u = call_usd(model, inp, out, cr, c5, c1, web)
        per_proj[pd] += u
        if pd == project_dir:
            per_agent[(sid, aid)] += u
    total = sum(per_proj.values())
    pp = lambda u: round(u / ratio, 1) if ratio else None
    for p in projs:
        u = per_proj.get(p["project_dir"], 0.0)
        p["win"] = dict(share=round(u / total, 3) if total else 0, pp=pp(u))
    for a in ags:
        u = per_agent.get((a["session_id"], a["agent"]), 0.0)
        a["win"] = dict(share=round(u / total, 3) if total else 0, pp=pp(u))
    return dict(start_ms=blk.start_ms, end_ms=blk.end_ms, source=blk.source, total_pp=pp(total), calibrated=bool(ratio))
