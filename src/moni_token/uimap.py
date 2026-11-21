"""UI panel of the agent map (issue #16, ADR-0007): only the UI that agents read or edited in the window.

The route table (Next.js app/ and pages/, SvelteKit, Flask/Jinja templates, static *.html) and the desktop UI sources
(XAML, WinForms *.Designer.cs) are a lookup table: "which UI does this file belong to". The panel itself is built from
the touches. Search scopes (Grep/Glob) never count. Everything here is read at request time and only shown.

    rendering      UI unit                      a read/edit touch of ... touches it
    static         the HTML file                that file
    Next.js app    route URL (/cart)            the route file, any file in its folder, a file it imports (one level)
    Next.js pages  route URL (/about)           the route file, a file it imports (one level)
    SvelteKit      route URL (/blog/[slug])     any file in the route folder (+page.*), a file it imports (one level)
    Flask/Jinja    route URL (@app.route)       the template the view passes to render_template
    XAML           named elements (x:Name)      the .xaml (all of them); its .xaml.cs (the names it mentions)
    WinForms       controls (Designer.cs)       the .Designer.cs (all of them); the form's .cs (the names it mentions)
"""
import os
import posixpath
import re
import xml.etree.ElementTree as ET
from pathlib import Path

PAGE_EXT = (".tsx", ".jsx", ".ts", ".js", ".mdx")
SVELTE_PAGE = ("+page.svelte", "+page.ts", "+page.js", "+page.server.ts", "+page.server.js")
JS_EXT = (".tsx", ".ts", ".jsx", ".js", ".svelte", ".mdx", ".vue")
HTML_EXT = (".html", ".htm")
DESKTOP_EXT = (".xaml", ".axaml")
MAX_PY_FILES = 300
MAX_BYTES = 512 * 1024
MAX_NODES = 400
MARKS_MAX = 20
NAME_MAX = 80

IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)['"]([^'"\n]+)['"]""")
ROUTE_DECO = re.compile(r"""^\s*@\w+(?:\.\w+)*\.(?:route|get|post|put|patch|delete)\(\s*['"]([^'"]+)['"]""")
DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+\w+")
RENDER = re.compile(r"""render_template\(\s*['"]([^'"]+)['"]""")
XNS = "{http://schemas.microsoft.com/winfx/2006/xaml}"
WF_NEW = re.compile(r"this\.(\w+)\s*=\s*new\s+([\w.]+)\s*\(")
WF_ADD = re.compile(r"this\.(?:(\w+)\.)?Controls\.Add\(\s*this\.(\w+)\s*\)")
WF_ADDRANGE = re.compile(r"this\.(?:(\w+)\.)?Controls\.AddRange\([^{]*\{([^}]*)\}")
WF_CLASS = re.compile(r"partial\s+class\s+(\w+)")
WORD = re.compile(r"\b[A-Za-z_]\w*\b")


def _group(seg: str) -> bool:
    return seg.startswith("(") and seg.endswith(")")


def _last(parts: list[str], name: str) -> int:
    return len(parts) - 1 - parts[::-1].index(name)


def route_table(entries: list[dict]) -> list[dict]:
    """Every page-like file in the tree, with the folder whose files belong to it (`dir`, None: only the file).
    This is the lookup table only; the panel lists what was touched."""
    out, seen = [], set()
    for e in entries:
        if e["dir"] or e.get("more"):
            continue
        p = e["path"]
        parts = p.split("/")
        name = parts[-1]
        stem, ext = posixpath.splitext(name)
        if "routes" in parts and name in SVELTE_PAGE:                       # SvelteKit
            segs = [s for s in parts[_last(parts, "routes") + 1:-1] if not _group(s)]
            pg = dict(route="/" + "/".join(segs), file=p, dir="/".join(parts[:-1]), kind="sveltekit")
        elif "app" in parts and stem == "page" and ext in PAGE_EXT:         # Next.js app router
            segs = [s for s in parts[_last(parts, "app") + 1:-1] if not _group(s)]
            pg = dict(route="/" + "/".join(segs), file=p, dir="/".join(parts[:-1]), kind="next-app")
        elif "pages" in parts and ext in PAGE_EXT and not stem.startswith("_"):   # Next.js pages router
            segs = parts[_last(parts, "pages") + 1:-1] + ([] if stem == "index" else [stem])
            if segs[:1] == ["api"]:
                continue
            pg = dict(route="/" + "/".join(segs), file=p, dir=None, kind="next-pages")
        elif ext.lower() in HTML_EXT:
            pg = dict(route=p, file=p, dir=None, kind="html")
        else:
            continue
        if (pg["route"], pg["kind"], pg["dir"]) in seen:   # +page.svelte and +page.ts of one route
            continue
        seen.add((pg["route"], pg["kind"], pg["dir"]))
        out.append(pg)
    return sorted(out, key=lambda x: x["route"])


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def flask_routes(text: str) -> dict[str, list[str]]:
    """template name -> routes, from `@x.route("/r")` (or .get/.post ...) views that call render_template("t.html")."""
    out: dict[str, list[str]] = {}
    lines = text.splitlines()
    pending: list[str] = []
    i = 0
    while i < len(lines):
        m = ROUTE_DECO.match(lines[i])
        if m:
            pending.append(m.group(1))
            i += 1
            continue
        d = DEF.match(lines[i])
        if d and pending:
            ind = len(d.group(1))
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > ind):
                for tpl in RENDER.findall(lines[j]):
                    for r in pending:
                        if r not in out.setdefault(tpl, []):
                            out[tpl].append(r)
                j += 1
            pending = []
            i = j
            continue
        if lines[i].strip() and not lines[i].lstrip().startswith("@"):
            pending = []
        i += 1
    return out


def xaml_nodes(text: str) -> list[dict]:
    """Root element plus every element with x:Name / Name / AutomationProperties.AutomationId, as a flat tree."""
    try:
        top = ET.fromstring(text)
    except ET.ParseError:
        return []
    out: list[dict] = []

    def walk(el, d):
        if len(out) >= MAX_NODES:
            return
        tag = el.tag.split("}")[-1]
        if "." in tag:          # a property element (Grid.RowDefinitions): not a control
            for c in el:
                walk(c, d)
            return
        name = el.get(XNS + "Name") or el.get("Name") or el.get("AutomationProperties.AutomationId")
        if el is top or name:
            cls = el.get(XNS + "Class")
            out.append(dict(d=d, type=tag, name=(name or (cls.split(".")[-1] if cls else ""))[:NAME_MAX]))
            d += 1
        for c in el:
            walk(c, d)
    walk(top, 0)
    return out


def designer_nodes(text: str) -> list[dict]:
    """The form and its control hierarchy from a WinForms *.Designer.cs (this.x = new T(); ...Controls.Add(this.x))."""
    types = {}
    for n, t in WF_NEW.findall(text):
        types.setdefault(n, t.split(".")[-1])
    kids: dict[str, list[str]] = {}
    for par, child in WF_ADD.findall(text):
        kids.setdefault(par, []).append(child)
    for par, body in WF_ADDRANGE.findall(text):
        kids.setdefault(par, []).extend(re.findall(r"this\.(\w+)", body))
    m = WF_CLASS.search(text)
    out = [dict(d=0, type="Form", name=(m.group(1) if m else ""))]
    seen = set()

    def walk(par, d):
        for c in kids.get(par, []):
            if c in seen or len(out) >= MAX_NODES:
                continue
            seen.add(c)
            out.append(dict(d=d, type=types.get(c, "Control"), name=c[:NAME_MAX]))
            walk(c, d + 1)
    walk("", 1)
    return out


class Lookup:
    """Which UI a project path belongs to. Built once per request from the tree entries; reads source lazily."""

    def __init__(self, root: Path | None, entries: list[dict]):
        self.root = root
        self.entries = entries
        self.files = {e["path"] for e in entries if not e["dir"] and not e.get("more")}
        self.pages = route_table(entries)
        self.desktop = sorted(p for p in self.files
                              if p.lower().endswith(DESKTOP_EXT) or p.lower().endswith(".designer.cs"))
        self._imports: dict[str, list[dict]] | None = None
        self._flask: dict[str, list[str]] | None = None

    @property
    def has_ui(self) -> bool:
        return bool(self.pages or self.desktop)

    def _exists(self, rel: str) -> bool:
        return rel in self.files or (self.root is not None and os.path.isfile(self.root / rel))

    def _resolve(self, src: str, spec: str) -> str | None:
        if spec.startswith(("./", "../")):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(src), spec))
        elif spec.startswith("@/"):
            base = spec[2:]
            if not any(self._exists(base + e) or self._exists(f"{base}/index{e}") for e in ("",) + JS_EXT):
                base = "src/" + base
        elif spec.startswith("$lib/"):
            base = "src/lib/" + spec[5:]
        else:
            return None
        if base.startswith(".."):
            return None
        for cand in [base] + [base + e for e in JS_EXT] + [f"{base}/index{e}" for e in JS_EXT]:
            if cand in self.files or (self.root is not None and cand != base and os.path.isfile(self.root / cand)):
                return cand
        return None

    def imports(self) -> dict[str, list[dict]]:
        """file -> pages whose route file imports it (one level; relative, `@/` and `$lib/` specifiers)."""
        if self._imports is None:
            self._imports = {}
            for pg in self.pages:
                if pg["kind"] == "html" or self.root is None:
                    continue
                text = _read(self.root / pg["file"]) or ""
                for spec in IMPORT.findall(text):
                    f = self._resolve(pg["file"], spec)
                    if f and pg not in self._imports.setdefault(f, []):
                        self._imports[f].append(pg)
        return self._imports

    def flask(self) -> dict[str, list[str]]:
        """template path (as in the tree) -> routes. Read only when a template was touched."""
        if self._flask is None:
            self._flask = {}
            if self.root is not None:
                names: dict[str, list[str]] = {}
                for p in sorted(f for f in self.files if f.endswith(".py"))[:MAX_PY_FILES]:
                    text = _read(self.root / p)
                    if text and "render_template" in text:
                        for tpl, rs in flask_routes(text).items():
                            names.setdefault(tpl, []).extend(r for r in rs if r not in names.get(tpl, []))
                for f in self.files:
                    if "templates" in f.split("/")[:-1]:
                        parts = f.split("/")
                        rel = "/".join(parts[_last(parts[:-1], "templates") + 1:])
                        if rel in names:
                            self._flask[f] = names[rel]
        return self._flask

    def pages_of(self, path: str) -> list[dict]:
        """The web UI entries (route or HTML file) a touched path belongs to; [] if none."""
        if "templates" in path.split("/")[:-1] and path.lower().endswith(HTML_EXT):
            routes = self.flask().get(path)
            if routes:
                return [dict(route=r, file=path, dir=None, kind="flask") for r in routes]
        exact = [pg for pg in self.pages if pg["file"] == path]
        if exact:
            return exact
        best = None
        for pg in self.pages:
            d = pg["dir"]
            if d is not None and (path == d or path.startswith(d + "/")) and (best is None or len(d) > len(best["dir"])):
                best = pg
        if best:
            return [best]
        if path.endswith(JS_EXT):
            return list(self.imports().get(path, []))
        return []

    def desktop_of(self, path: str) -> tuple[str, str | None] | None:
        """(UI source file, code file whose mentioned names are touched; None = every element) or None."""
        low = path.lower()
        if path in self.desktop:
            return path, None
        for ext in DESKTOP_EXT:
            if low.endswith(ext + ".cs") and path[:-3] in self.desktop:
                return path[:-3], path
        if low.endswith(".cs") and not low.endswith(".designer.cs"):
            d = path[:-3] + ".Designer.cs"
            if d in self.desktop:
                return d, path
        return None


def _mark(a: dict, t: dict) -> dict:
    return dict(a=a["key"], t=t["t"], tool=t["tool"], edit=t["edit"], path=t["path"])


def panel(look: Lookup, ags: list[dict]) -> dict:
    """The panel: web entries (route or HTML file) and desktop element trees, built from read/edit touches only.
    Sets t["page"] (the first route) on each touch for the agent cards."""
    web: dict[tuple, dict] = {}
    desk: dict[str, dict] = {}
    for a in ags:
        for t in a["touches"]:
            t["page"] = None
            if t["kind"] == "search":        # a search scope is not work on a UI (issue #15)
                continue
            for pg in look.pages_of(t["path"]):
                t["page"] = t["page"] or pg["route"]
                w = web.setdefault((pg["route"], pg["kind"]), dict(route=pg["route"], kind=pg["kind"], files=[],
                                                                   marks=[], edit=False, last=0))
                if t["path"] not in w["files"]:
                    w["files"].append(t["path"])
                w["marks"].append(_mark(a, t))
                w["edit"] |= t["edit"]
                w["last"] = max(w["last"], t["t"])
            own = look.desktop_of(t["path"])
            if own:
                src, code = own
                d = desk.setdefault(src, dict(file=src, kind="winforms" if src.lower().endswith(".designer.cs")
                                              else "xaml", codes=set(), all=False, marks=[], edit=False, last=0,
                                              files=[]))
                if code is None:
                    d["all"] = True
                else:
                    d["codes"].add(code)
                if t["path"] not in d["files"]:
                    d["files"].append(t["path"])
                d["marks"].append(_mark(a, t))
                d["edit"] |= t["edit"]
                d["last"] = max(d["last"], t["t"])
    desktop = []
    for d in desk.values():
        text = _read(look.root / d["file"]) if look.root is not None else None
        nodes = (designer_nodes(text) if d["kind"] == "winforms" else xaml_nodes(text)) if text else []
        words: set[str] = set()
        for c in d.pop("codes"):
            words |= set(WORD.findall(_read(look.root / c) or "")) if look.root is not None else set()
        for i, n in enumerate(nodes):
            n["on"] = d["all"] or (i > 0 and n["name"] in words)
        if nodes:
            nodes[0]["on"] = True   # the window/form whose source or code was touched
        d.pop("all")
        d["nodes"] = nodes
        d["marks"] = d["marks"][-MARKS_MAX:]
        desktop.append(d)
    entries = sorted(web.values(), key=lambda w: -w["last"])
    for w in entries:
        w["marks"] = w["marks"][-MARKS_MAX:]
    return dict(has_ui=look.has_ui, has_desktop=bool(look.desktop), web=entries,
                desktop=sorted(desktop, key=lambda d: -d["last"]))


def touched_names(desktop: list[dict]) -> set[str]:
    """Element names checked in the source trees: an OS UI element with that AutomationId is checked too."""
    return {n["name"] for d in desktop for n in d["nodes"] if n["on"] and n["name"]}
