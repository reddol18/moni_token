"""Opt-in Windows UI Automation reader for the agent map's UI panel (issue #16, ADR-0007).

Off unless ~/.moni_token/config.toml has `[map] uia = true`. Reads the element tree (control type, AutomationId,
name) of one top-level window: the first whose process executable lies under the project root, else the first whose
title contains `uia_window`. No new dependency: the native UI Automation COM API (UIAutomationCore, part of Windows),
called through a few [ComImport] declarations compiled by PowerShell's Add-Type in a
`powershell -NoProfile -NonInteractive -EncodedCommand` child process with a timeout, so a hung UI cannot stop the map.
(The managed System.Windows.Automation client was tried first: on Windows 10 it returned no names and saw Win32/WinForms
controls only as panes.) Element names and window titles can hold user content: they are returned for the page only,
kept in memory for TTL_S seconds and never written anywhere.
"""
import base64
import json
import os
import subprocess
import threading
import time

TTL_S = 15.0
TIMEOUT_S = 10.0
MAX_NODES = 400
MAX_DEPTH = 8
NAME_MAX = 80

# UIA_*ControlTypeId -> name (the localized name the API offers would follow the OS language)
CONTROL_TYPES = dict(enumerate((
    "Button", "Calendar", "CheckBox", "ComboBox", "Edit", "Hyperlink", "Image", "ListItem", "List", "Menu", "MenuBar",
    "MenuItem", "ProgressBar", "RadioButton", "ScrollBar", "Slider", "Spinner", "StatusBar", "Tab", "TabItem", "Text",
    "ToolBar", "ToolTip", "Tree", "TreeItem", "Custom", "Group", "Thumb", "DataGrid", "DataItem", "Document",
    "SplitButton", "Window", "Pane", "Header", "HeaderItem", "Table", "TitleBar", "Separator", "SemanticZoom", "AppBar"),
    start=50000))

SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$src = @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
namespace MoniUia {
  [ComImport, Guid("d22108aa-8ac5-49a5-837b-37bbb3d7591e"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IElement {
    void _0(); void _1(); void _2(); void _3(); void _4(); void _5(); void _6();
    [return: MarshalAs(UnmanagedType.Struct)] object GetCurrentPropertyValue(int id);
  }
  [ComImport, Guid("4042c624-389c-4afc-a630-9df854a541fc"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IWalker {
    IElement GetParentElement(IElement e); IElement GetFirstChildElement(IElement e);
    IElement GetLastChildElement(IElement e); IElement GetNextSiblingElement(IElement e);
  }
  [ComImport, Guid("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IAuto {
    void _0(); void _1(); IElement GetRootElement(); IElement ElementFromHandle(IntPtr h);
    void _4(); void _5(); void _6(); void _7(); void _8(); void _9(); void _10();
    IWalker ControlViewWalker { get; }
  }
  public static class Reader {
    const int PID = 30002, TYPE = 30003, NAME = 30005, AID = 30011;
    static string S(IElement e, int id) { object v = e.GetCurrentPropertyValue(id); return v == null ? "" : v.ToString(); }
    static int P(IElement e) { return Convert.ToInt32(e.GetCurrentPropertyValue(PID)); }
    static string Exe(int pid) { try { return Process.GetProcessById(pid).MainModule.FileName; } catch { return ""; } }
    static string Proc(int pid) { try { return Process.GetProcessById(pid).ProcessName; } catch { return ""; } }
    public static Hashtable Read(string root, string title, int max, int depth) {
      IAuto a = (IAuto)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("ff48dba4-60ef-4201-aa87-54103eef594e")));
      IWalker w = a.ControlViewWalker;
      var tops = new List<IElement>();
      for (IElement c = w.GetFirstChildElement(a.GetRootElement()); c != null; c = w.GetNextSiblingElement(c)) tops.Add(c);
      IElement win = null; string by = null;
      if (!String.IsNullOrEmpty(root)) {
        string pre = root.TrimEnd('\\') + "\\";
        foreach (var t in tops)
          if (Exe(P(t)).StartsWith(pre, StringComparison.OrdinalIgnoreCase)) { win = t; by = "path"; break; }
      }
      if (win == null && !String.IsNullOrEmpty(title))
        foreach (var t in tops)
          if (S(t, NAME).IndexOf(title, StringComparison.OrdinalIgnoreCase) >= 0) { win = t; by = "title"; break; }
      var res = new Hashtable();
      res["window"] = null;
      if (win == null) return res;
      var nodes = new ArrayList();
      Walk(w, win, 0, max, depth, nodes);
      res["window"] = Proc(P(win)); res["pid"] = P(win); res["title"] = S(win, NAME); res["by"] = by;
      res["nodes"] = nodes;
      return res;
    }
    static void Walk(IWalker w, IElement e, int d, int max, int depth, ArrayList nodes) {
      if (nodes.Count >= max) return;
      var n = new Hashtable();
      n["d"] = d; n["ct"] = S(e, TYPE); n["name"] = S(e, NAME); n["aid"] = S(e, AID);
      nodes.Add(n);
      if (d >= depth) return;
      for (IElement c = w.GetFirstChildElement(e); c != null && nodes.Count < max; c = w.GetNextSiblingElement(c))
        Walk(w, c, d + 1, max, depth, nodes);
    }
  }
}
'@
Add-Type -TypeDefinition $src
$r = [MoniUia.Reader]::Read($env:MONI_UIA_ROOT, $env:MONI_UIA_TITLE, [int]$env:MONI_UIA_MAX, [int]$env:MONI_UIA_DEPTH)
$r | ConvertTo-Json -Depth 5 -Compress
"""

_cache: dict[tuple, tuple[float, dict]] = {}
_busy: set[tuple] = set()
_lock = threading.Lock()


def command() -> list[str]:
    enc = base64.b64encode(SCRIPT.encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]


def parse(raw: bytes | str) -> dict:
    """The child's JSON -> dict(window, pid, title, by, nodes=[dict(d, type, name, aid)]) or dict(window=None)."""
    try:
        d = json.loads(raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw)
    except ValueError:
        return dict(window=None, error="parse")
    if not isinstance(d, dict) or not d.get("window"):
        return dict(window=None)
    nodes = d.get("nodes") or []
    if isinstance(nodes, dict):   # ConvertTo-Json writes a one-element array as an object
        nodes = [nodes]
    out = []
    for n in nodes[:MAX_NODES]:
        if not isinstance(n, dict):
            continue
        ct = str(n.get("ct") or "")
        typ = CONTROL_TYPES.get(int(ct), ct) if ct.isdigit() else str(n.get("type") or ct)
        out.append(dict(d=int(n.get("d") or 0), type=typ, name=str(n.get("name") or "")[:NAME_MAX],
                        aid=str(n.get("aid") or "")[:NAME_MAX]))
    return dict(window=str(d["window"]), pid=d.get("pid"), title=str(d.get("title") or "")[:NAME_MAX],
                by=d.get("by"), nodes=out)


def _run(root: str, title: str, run) -> dict:
    if os.name != "nt":
        return dict(window=None, error="unsupported")
    env = dict(os.environ, MONI_UIA_ROOT=root or "", MONI_UIA_TITLE=title or "", MONI_UIA_MAX=str(MAX_NODES),
               MONI_UIA_DEPTH=str(MAX_DEPTH))   # values go through the environment, not the command line
    try:
        r = run(command(), env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT_S,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return dict(window=None, error="failed")
    if r.returncode != 0:
        return dict(window=None, error="failed")
    return parse(r.stdout)


def _refresh(key: tuple, run, at: float) -> dict:
    t0 = time.perf_counter()
    res = _run(key[0], key[1], run)
    res["ms"] = round((time.perf_counter() - t0) * 1000)
    with _lock:
        _cache[key] = (at, res)
        _busy.discard(key)
    return res


def read_window(root: str, title: str | None = None, run=subprocess.run, clock=time.monotonic,
                background: bool = False) -> dict:
    """The element tree of the project's running window, cached in memory for TTL_S seconds.

    With `background` (the map server) a stale or missing entry is refreshed on a thread and the last result (or
    dict(window=None, pending=True)) is returned at once, so a page poll never waits for PowerShell."""
    key = (root, title or "")
    now = clock()
    with _lock:
        hit = _cache.get(key)
        if (hit is not None and now - hit[0] < TTL_S) or (background and key in _busy):
            return hit[1] if hit else dict(window=None, pending=True)
        _busy.add(key)
    if not background:
        return _refresh(key, run, now)
    threading.Thread(target=_refresh, args=(key, run, now), daemon=True).start()
    return hit[1] if hit else dict(window=None, pending=True)


def mark(res: dict, names: set[str]) -> dict:
    """A copy of the tree with `on` set where the AutomationId is a touched source element's name."""
    return dict(res, nodes=[dict(n, on=bool(n["aid"]) and n["aid"] in names) for n in res.get("nodes", [])])
