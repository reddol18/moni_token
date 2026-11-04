import json
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from conftest import SECRET
from moni_token import agentmap, mapserver
from moni_token.collector import collect
from moni_token.parser import rel_path


def test_rel_path_only_inside_project():
    root = r"E:\work\shop"
    assert rel_path(r"E:\work\shop\src\a.py", root) == "src/a.py"
    assert rel_path(r"e:\WORK\shop\src\a.py", root) == "src/a.py"            # Windows: case-insensitive
    assert rel_path(r"E:\work\shop", root) == ""
    assert rel_path("src/a.py", root) == "src/a.py"                          # relative input
    for outside in (r"E:\work\other\a.py", r"C:\Users\me\.ssh\id_rsa", r"E:\work\shop\..\x.py", r"\\srv\share\a"):
        assert rel_path(outside, root) is None, outside
    assert rel_path("/home/me/shop/a/b.ts", "/home/me/shop") == "a/b.ts"
    assert rel_path("/etc/passwd", "/home/me/shop") is None
    assert rel_path(None, root) is None and rel_path("a", None) is None


def _call(msg_id, ts, cwd, tools, session="s1", agent=None):
    content = [{"type": "text", "text": SECRET}] + [
        {"type": "tool_use", "id": f"t{msg_id}{i}", "name": n, "input": inp} for i, (n, inp) in enumerate(tools)]
    return {"type": "assistant", "uuid": f"u{msg_id}", "timestamp": ts, "sessionId": session, "cwd": cwd,
            "isSidechain": agent is not None, "agentId": agent,
            "message": {"id": msg_id, "model": "claude-opus-5", "role": "assistant", "content": content,
                        "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 100}}}


def iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ms / 1000))


def test_db_keeps_only_project_relative_paths(logs, con, tmp_path):
    root = str(tmp_path / "shop")
    logs.write(logs.path(), _call("m1", "2026-11-05T01:00:00.000Z", root, [
        ("Edit", {"file_path": root + "/src/a.py", "old_string": SECRET, "new_string": SECRET}),
        ("Read", {"file_path": str(tmp_path / "secret-elsewhere.txt")}),
        ("Bash", {"command": SECRET}),
        ("Grep", {"pattern": SECRET, "path": root + "/src"})]))
    collect(con, logs.root)
    rows = sorted(con.execute("SELECT name, path FROM call_tools").fetchall())
    assert rows == [("Bash", None), ("Edit", "src/a.py"), ("Grep", "src"), ("Read", None)]
    dump = "\n".join(str(r) for t in ("calls", "call_tools") for r in con.execute(f"SELECT * FROM {t}"))
    assert SECRET not in dump and "secret-elsewhere" not in dump


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "shop"
    for f in ("app/(store)/cart/page.tsx", "app/(store)/cart/CartList.tsx", "app/page.tsx", "pages/about.tsx",
              "pages/api/x.ts", "src/lib/price.py", "src/lib/tax.py", "node_modules/big/index.js", "README.md"):
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text("x", encoding="utf-8")
    (root / "vendor/other/.git").mkdir(parents=True)                   # nested repository: not part of the tree
    (root / "vendor/other/index.html").write_text("x", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs/PLAN.md").write_text(
        "# Plan\n\n## Checkout\nTouches `app/(store)/cart/` and `price.py`.\n- [x] list items\n- [ ] pay\n\n"
        "## Notes\nnothing linked\n\n## Tax\n`src/lib/tax.py`\n", encoding="utf-8")
    return root


def test_state_maps_agents_to_tree_pages_and_features(logs, con, project):
    now = int(time.time() * 1000)
    r = str(project)
    logs.write(logs.path(session="s1"), _call("m1", iso(now - 600_000), r, [("Read", {"file_path": r + "/README.md"})]))
    sub = logs.path(session="s1", agent="abc")
    logs.write(sub, _call("m2", iso(now - 30_000), r, [("Edit", {"file_path": r + "/app/(store)/cart/CartList.tsx"})],
                          agent="abc"))
    sub.with_name("agent-abc.meta.json").write_text(json.dumps({"agentType": "Explore", "description": SECRET}),
                                                    encoding="utf-8")
    collect(con, logs.root)
    st = agentmap.build_state(con, logs.root, now)
    assert st["project"]["root"] == r
    paths = {e["path"] for e in st["tree"]}
    assert "app/(store)/cart/page.tsx" in paths and not any(p.startswith("node_modules") for p in paths)
    assert "vendor" in paths and not any(p.startswith("vendor/other") for p in paths)
    routes = {p["route"]: p["kind"] for p in st["pages"]}
    assert routes == {"/": "next-app", "/cart": "next-app", "/about": "next-pages"}      # api/ is not a page
    feats = {f["title"]: f for f in st["features"]}
    assert set(feats) == {"Checkout", "Tax"}                                              # "Notes" links nothing
    assert feats["Checkout"]["paths"] == ["app/(store)/cart", "src/lib/price.py"] and feats["Checkout"]["done"] == 1
    sa = next(a for a in st["agents"] if a["agent"] == "abc")
    assert sa["type"] == "Explore" and sa["status"] == "running" and sa["where"] == "app/(store)/cart/CartList.tsx"
    t = sa["touches"][-1]
    assert t["edit"] and t["page"] == "/cart" and [st["features"][i]["title"] for i in t["features"]] == ["Checkout"]
    main = next(a for a in st["agents"] if a["agent"] is None)
    assert main["type"] == "main" and main["status"] == "idle"
    assert SECRET not in json.dumps(st, ensure_ascii=False)                               # meta description not read


def test_past_moment_ignores_later_calls(logs, con, project):
    r = str(project)
    t0 = int(time.time() * 1000) - 86_400_000
    logs.write(logs.path(), _call("m1", iso(t0), r, [("Read", {"file_path": r + "/README.md"})]),
               _call("m2", iso(t0 + 3_600_000), r, [("Edit", {"file_path": r + "/src/lib/tax.py"})]))
    collect(con, logs.root)
    a = agentmap.build_state(con, logs.root, t0 + 30_000)["agents"]
    assert len(a) == 1 and a[0]["calls"] == 1 and a[0]["status"] == "running" and a[0]["where"] == "README.md"


def test_server_is_loopback_only_and_checks_host(logs, tmp_path):
    assert mapserver.HOST == "127.0.0.1"
    srv = ThreadingHTTPServer((mapserver.HOST, 0), mapserver.make_handler(tmp_path / "m.db", logs.root))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]

        def get(path, host=None):
            c = HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", path, headers={"Host": host} if host else {})
            r = c.getresponse()
            return r.status, r.read()

        status, body = get("/")
        assert status == 200 and b"http://" not in body.replace(b"http://127.0.0.1", b"")   # no external resources
        status, body = get("/api/state")
        assert status == 200 and json.loads(body)["agents"] == []
        assert get("/", host="evil.example:80")[0] == 403
    finally:
        srv.shutdown()
        srv.server_close()
