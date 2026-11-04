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

from .parser import rel_path
from .units import call_usd

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build", ".next",
             ".turbo", ".cache", "coverage", ".idea", ".vscode", ".mypy_cache", ".ruff_cache", "target"}
MAX_ENTRIES = 4000
RUNNING_MS = 2 * 60_000
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CONTRACT_GLOBS = ("docs/PLAN.md", "PLAN.md", "SPEC.md", "docs/SPEC.md", "docs/tasks/*.md")
PAGE_EXT = (".tsx", ".jsx", ".ts", ".js", ".mdx")


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
    """agentType / finished / parent from each subagent's .meta.json. The free-text description is not read."""
    out = {}
    base = projects / project_dir / (session_id or "") / "subagents"
    if session_id and base.is_dir():
        for m in base.rglob("agent-*.meta.json"):
            try:
                d = json.loads(m.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            aid = m.name[len("agent-"):-len(".meta.json")]
            out[aid] = dict(type=str(d.get("agentType") or ""), finished=bool(d.get("finished")),
                            parent=d.get("parentAgentId"), depth=d.get("spawnDepth"))
    return out


def agents(con: sqlite3.Connection, projects: Path, project_dir: str, root: str, since_ms: int, now_ms: int) -> list[dict]:
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
        # recent calls first: viewing a past moment (?at=) must not show today's "finished" flag
        a["status"] = "running" if now_ms - a["last_ms"] < RUNNING_MS else ("done" if m.get("finished") else "idle")
        a["touches"], a["tool"], a["where"] = [], None, None
    for sid, aid, ts, cwd, name, path in con.execute(
            "SELECT c.session_id, c.agent_id, c.ts_ms, c.cwd, t.name, t.path FROM call_tools t JOIN calls c "
            "USING(msg_id) WHERE c.project_dir=? AND c.ts_ms >= ? AND c.ts_ms <= ? ORDER BY c.ts_ms",
            (project_dir, since_ms, now_ms)):
        a = per.get((sid, aid))
        if not a:
            continue
        a["tool"] = name
        if path is None:
            continue
        p = path if (not cwd or cwd == root) else rel_path(_join(cwd, path), root)   # subfolder cwd -> project root
        if p is None:
            continue
        a["touches"].append(dict(t=ts, path=p, edit=name in EDIT_TOOLS, tool=name))
        a["where"] = p
    out = sorted(per.values(), key=lambda a: (a["status"] != "running", -a["last_ms"]))
    for i, a in enumerate(out):
        a["key"] = f"a{i}"
        a["usd"] = round(a["usd"], 3)
    return out


def tree(root: Path) -> list[dict]:
    """Directory entries (names only) under root, breadth-limited by MAX_ENTRIES."""
    out: list[dict] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and (not d.startswith(".") or d == ".github")
                             and not (Path(dirpath, d, ".git").exists()))   # a nested repository is another project
        rel = Path(dirpath).relative_to(root).as_posix()
        rel = "" if rel == "." else rel
        for d in dirnames:
            out.append(dict(path=f"{rel}/{d}" if rel else d, dir=True))
        for f in sorted(filenames):
            out.append(dict(path=f"{rel}/{f}" if rel else f, dir=False))
        if len(out) >= MAX_ENTRIES:
            return out[:MAX_ENTRIES]
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
                window_min: int = 30) -> dict:
    since = now_ms - window_min * 60_000
    projs = active_projects(con, since, now_ms)
    cur = next((p for p in projs if p["project_dir"] == project_dir), projs[0] if projs else None)
    state = dict(now=now_ms, window_min=window_min, projects=projs, project=cur, agents=[], tree=[], pages=[],
                 features=[])
    if not cur:
        return state
    root = cur["root"]
    ags = agents(con, projects, cur["project_dir"], root, since, now_ms) if root else []
    entries = tree(Path(root)) if root else []
    pgs = pages(entries)
    feats = features(Path(root), entries) if root else []
    for a in ags:
        for t in a["touches"]:
            pg = page_of(t["path"], pgs)
            t["page"] = pg["route"] if pg else None
            t["features"] = features_of(t["path"], feats)
        a["touches"] = a["touches"][-200:]
    state.update(agents=ags, tree=entries, pages=pgs, features=feats)
    return state
