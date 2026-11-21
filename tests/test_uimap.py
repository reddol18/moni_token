"""Issue #16 (ADR-0007): the UI panel lists only UI read or edited in the window."""
import json
import subprocess
import time

import pytest

from conftest import SECRET
from moni_token import agentmap, mapserver, uia, uimap
from moni_token.collector import collect
from moni_token.settings import MapParams, load_map_params
from test_agentmap import _call, iso


def make(root, files: dict):
    for f, text in files.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text, encoding="utf-8")
    return root


def run_panel(root, touches):
    """touches: [(path, kind)] by one agent; returns the panel."""
    entries = agentmap.tree(root, {p for p, _ in touches})
    ags = [dict(key="a0", type="main", touches=[dict(t=1000 + i, path=p, kind=k, edit=k == "edit",
                                                     tool={"edit": "Edit", "read": "Read", "search": "Grep"}[k])
                                                for i, (p, k) in enumerate(touches)])]
    return uimap.panel(uimap.Lookup(root, entries), ags), ags


def test_static_many_html_one_edited_gives_exactly_one_entry(tmp_path):
    """fund_agent 2026-11-21: dozens of backup/artifacts-*/...html filled the panel though nobody worked on them."""
    root = tmp_path / "fund"
    files = {f"backup/artifacts-{i:02d}/0{j}_report.html": "<html></html>" for i in range(12) for j in range(4)}
    files.update({"docs/artifacts/05_screening.html": "<html></html>", "docs/artifacts/06_other.html": "x",
                  "src/run.py": "print(1)"})
    make(root, files)
    ui, ags = run_panel(root, [("docs/artifacts/05_screening.html", "edit"), ("backup", "search"),
                               ("src/run.py", "read")])
    assert [(w["route"], w["kind"], w["edit"]) for w in ui["web"]] == [("docs/artifacts/05_screening.html", "html",
                                                                         True)]
    assert ui["web"][0]["marks"][0]["a"] == "a0" and ui["web"][0]["marks"][0]["tool"] == "Edit"
    assert ui["has_ui"] and ui["desktop"] == []
    assert [t["page"] for t in ags[0]["touches"]] == ["docs/artifacts/05_screening.html", None, None]


def test_search_scope_on_a_page_is_not_a_touch(tmp_path):
    root = make(tmp_path / "s", {"index.html": "x"})
    ui, _ = run_panel(root, [("index.html", "search")])
    assert ui["web"] == [] and ui["has_ui"]


def test_empty_states_no_ui_in_project_vs_none_touched(tmp_path):
    root = make(tmp_path / "lib", {"src/a.py": "x", "README.md": "x"})
    ui, _ = run_panel(root, [("src/a.py", "edit")])
    assert not ui["has_ui"] and ui["web"] == [] and ui["desktop"] == []
    root2 = make(tmp_path / "site", {"index.html": "x", "src/a.py": "x"})
    ui2, _ = run_panel(root2, [("src/a.py", "edit")])
    assert ui2["has_ui"] and ui2["web"] == []


def test_nextjs_component_maps_to_its_route(tmp_path):
    root = make(tmp_path / "shop", {
        "app/(store)/cart/page.tsx": "import CartList from '@/components/CartList'\nimport x from 'react'\n",
        "app/(store)/cart/Summary.tsx": "x",
        "app/page.tsx": "import { Hero } from '../components/Hero';\n",
        "components/CartList.tsx": "x", "components/Hero/index.tsx": "x", "components/Unused.tsx": "x",
        "pages/about.tsx": "import Bio from './_bio'\n", "pages/_bio.tsx": "x",
    })
    ui, ags = run_panel(root, [("components/CartList.tsx", "edit"), ("app/(store)/cart/Summary.tsx", "read"),
                               ("components/Hero/index.tsx", "read"), ("pages/_bio.tsx", "read"),
                               ("components/Unused.tsx", "edit")])
    got = {w["route"]: (w["kind"], sorted(w["files"])) for w in ui["web"]}
    assert got == {"/cart": ("next-app", ["app/(store)/cart/Summary.tsx", "components/CartList.tsx"]),
                   "/": ("next-app", ["components/Hero/index.tsx"]),
                   "/about": ("next-pages", ["pages/_bio.tsx"])}
    assert ags[0]["touches"][0]["page"] == "/cart" and ags[0]["touches"][-1]["page"] is None


def test_sveltekit_routes(tmp_path):
    root = make(tmp_path / "blog", {
        "src/routes/+page.svelte": "<script>import Nav from '$lib/Nav.svelte';</script>",
        "src/routes/blog/[slug]/+page.svelte": "x", "src/routes/blog/[slug]/+page.ts": "x",
        "src/routes/blog/[slug]/Comment.svelte": "x", "src/routes/(auth)/login/+page.svelte": "x",
        "src/lib/Nav.svelte": "x"})
    table = {(p["route"], p["kind"]) for p in uimap.route_table(agentmap.tree(root))}
    assert table == {("/", "sveltekit"), ("/blog/[slug]", "sveltekit"), ("/login", "sveltekit")}
    ui, _ = run_panel(root, [("src/routes/blog/[slug]/Comment.svelte", "edit"), ("src/lib/Nav.svelte", "read")])
    assert {w["route"] for w in ui["web"]} == {"/blog/[slug]", "/"}


def test_flask_templates_map_to_routes(tmp_path):
    root = make(tmp_path / "flaskapp", {
        "app.py": ('from flask import Flask, render_template\napp = Flask(__name__)\n\n'
                   '@app.route("/")\ndef index():\n    return render_template("index.html")\n\n'
                   '@app.get("/settings/billing")\n@login_required\ndef billing():\n    x = 1\n\n'
                   '    return render_template("settings/billing.html", x=x)\n\n'
                   'def helper():\n    return render_template("orphan.html")\n'),
        "templates/index.html": "x", "templates/settings/billing.html": "x", "templates/orphan.html": "x"})
    ui, _ = run_panel(root, [("templates/settings/billing.html", "edit"), ("templates/orphan.html", "read")])
    got = {(w["route"], w["kind"]) for w in ui["web"]}
    assert got == {("/settings/billing", "flask"), ("templates/orphan.html", "html")}
    assert uimap.flask_routes((root / "app.py").read_text()) == {"index.html": ["/"],
                                                                  "settings/billing.html": ["/settings/billing"]}


XAML = """<Window x:Class="Demo.MainWindow" xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
 xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" Title="TOP-SECRET-BODY-7f3a">
 <Grid x:Name="root"><Grid.RowDefinitions><RowDefinition/></Grid.RowDefinitions>
  <StackPanel><TextBox x:Name="amountBox" Text="TOP-SECRET-BODY-7f3a"/>
   <Button x:Name="saveButton" Content="Save"/><Button AutomationProperties.AutomationId="cancel"/></StackPanel>
 </Grid></Window>"""

DESIGNER = """namespace Demo { partial class BillingForm {
 private void InitializeComponent() {
  this.panel1 = new System.Windows.Forms.Panel();
  this.okButton = new System.Windows.Forms.Button();
  this.nameBox = new System.Windows.Forms.TextBox();
  this.tip = new System.Windows.Forms.ToolTip(this.components);
  this.panel1.Controls.Add(this.okButton);
  this.panel1.Controls.AddRange(new System.Windows.Forms.Control[] { this.nameBox });
  this.Controls.Add(this.panel1);
 } } }"""


def test_xaml_code_behind_checks_the_elements_it_mentions(tmp_path):
    root = make(tmp_path / "wpf", {"MainWindow.xaml": XAML,
                                    "MainWindow.xaml.cs": "void Save() { saveButton.IsEnabled = false; }",
                                    "Other.xaml": XAML})
    ui, _ = run_panel(root, [("MainWindow.xaml.cs", "edit")])
    [d] = ui["desktop"]
    assert d["file"] == "MainWindow.xaml" and d["kind"] == "xaml" and d["edit"]
    assert [(n["d"], n["type"], n["name"], n["on"]) for n in d["nodes"]] == [
        (0, "Window", "MainWindow", True), (1, "Grid", "root", False), (2, "TextBox", "amountBox", False),
        (2, "Button", "saveButton", True), (2, "Button", "cancel", False)]
    assert SECRET not in json.dumps(ui)                     # text/content attributes are never read
    ui2, _ = run_panel(root, [("Other.xaml", "read")])
    assert all(n["on"] for n in ui2["desktop"][0]["nodes"]) and ui2["web"] == []
    assert uimap.touched_names(ui["desktop"]) == {"MainWindow", "saveButton"}


def test_winforms_designer_tree(tmp_path):
    root = make(tmp_path / "wf", {"BillingForm.Designer.cs": DESIGNER, "BillingForm.cs": "okButton.Click += Ok;"})
    ui, _ = run_panel(root, [("BillingForm.cs", "edit")])
    [d] = ui["desktop"]
    assert d["kind"] == "winforms" and d["file"] == "BillingForm.Designer.cs"
    assert [(n["d"], n["type"], n["name"], n["on"]) for n in d["nodes"]] == [
        (0, "Form", "BillingForm", True), (1, "Panel", "panel1", False), (2, "Button", "okButton", True),
        (2, "TextBox", "nameBox", False)]                     # the ToolTip is not a control in the tree
    ui2, _ = run_panel(root, [("BillingForm.Designer.cs", "read")])
    assert all(n["on"] for n in ui2["desktop"][0]["nodes"])


def test_uia_parse_mark_and_cache(monkeypatch):
    raw = json.dumps({"window": "Demo", "pid": 42, "title": "t", "by": "path",
                      "nodes": {"d": 0, "type": "Window", "name": "Demo " + "x" * 200, "aid": ""}}).encode()
    r = uia.parse(raw)
    assert r["window"] == "Demo" and len(r["nodes"]) == 1 and len(r["nodes"][0]["name"]) == uia.NAME_MAX
    assert uia.parse(b'{"window":null}') == {"window": None} and uia.parse(b"oops")["error"] == "parse"
    marked = uia.mark(dict(window="w", nodes=[dict(d=0, type="Button", name="Save", aid="saveButton"),
                                              dict(d=0, type="Text", name="x", aid="")]), {"saveButton"})
    assert [n["on"] for n in marked["nodes"]] == [True, False]
    calls = []

    class R:
        returncode = 0
        stdout = raw

    def fake(cmd, **kw):
        calls.append((cmd, kw["env"]))
        return R()
    monkeypatch.setattr(uia, "_cache", {})
    monkeypatch.setattr(uia.os, "name", "nt")
    clock = iter([0.0, 5.0, 20.0]).__next__
    for _ in range(3):
        uia.read_window(r"E:\proj", "My Title", run=fake, clock=clock)
    assert len(calls) == 2                                  # 5 s later: cached; 20 s later: read again
    cmd, env = calls[0]
    assert cmd[0] == "powershell" and "-EncodedCommand" in cmd and "My Title" not in " ".join(cmd)
    assert env["MONI_UIA_TITLE"] == "My Title" and env["MONI_UIA_ROOT"] == r"E:\proj"

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 8)
    monkeypatch.setattr(uia, "_cache", {})
    assert uia.read_window("r", None, run=boom)["error"] == "failed"
    assert uia.parse(b'{"window":"a","nodes":[{"d":1,"ct":"50000","name":"n","aid":"x"}]}')["nodes"][0]["type"] == \
        "Button"                                            # control type ids, not the OS-language name

    # background (the server): a poll never waits; the first answer is "pending", the next one the tree
    monkeypatch.setattr(uia, "_cache", {})
    first = uia.read_window("bg", None, run=fake, background=True)
    assert first == {"window": None, "pending": True}
    for _ in range(100):
        if ("bg", "") in uia._cache:
            break
        time.sleep(0.01)
    assert uia.read_window("bg", None, run=fake, background=True)["window"] == "Demo"


def test_uia_is_opt_in_live_only_and_never_stored(logs, con, tmp_path, monkeypatch):
    root = make(tmp_path / "wpf", {"MainWindow.xaml": XAML, "MainWindow.xaml.cs": "saveButton.x();"})
    r = str(root)
    now = int(time.time() * 1000)
    logs.write(logs.path(), _call("u1", iso(now - 60_000), r, [("Edit", {"file_path": r + "/MainWindow.xaml.cs"})]))
    collect(con, logs.root)
    seen = []

    def fake(root_, title=None, **kw):
        seen.append((root_, title))
        return dict(window="Demo", pid=1, by="path", ms=5,
                    nodes=[dict(d=0, type="Button", name="Elem-" + SECRET, aid="saveButton"),
                           dict(d=0, type="Button", name="b", aid="other")])
    monkeypatch.setattr(uia, "read_window", fake)
    assert agentmap.build_state(con, logs.root, now)["ui"]["uia"] is None              # off by default
    st = agentmap.build_state(con, logs.root, now, map_params=MapParams(uia=True))
    assert seen == [(r, None)] and [n["on"] for n in st["ui"]["uia"]["nodes"]] == [True, False]
    assert st["ui"]["desktop"][0]["file"] == "MainWindow.xaml"
    past = agentmap.build_state(con, logs.root, now, live=False, map_params=MapParams(uia=True))
    assert past["ui"]["uia"] == {"window": None, "past": True} and len(seen) == 1
    dump = "\n".join(str(row) for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
                     for row in con.execute(f'SELECT * FROM "{t}"'))
    assert "Elem-" not in dump and "saveButton" not in dump


def test_map_params(tmp_path):
    p = tmp_path / "config.toml"
    assert load_map_params(p) == MapParams()
    p.write_text('[map]\nuia = true\nuia_window = "Billing"\n', encoding="utf-8")
    assert load_map_params(p) == MapParams(uia=True, uia_window="Billing")
    p.write_text('[map]\nuia = "yes"\n', encoding="utf-8")
    assert load_map_params(p).uia is False


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_page_renders_the_touched_ui_panel(lang):
    html = mapserver.page(lang)
    for s in ("function uiPanel(", "p.m.ui_none_touched", "p.m.ui_none_found", "data-go=", "goTree(e.dataset.go",
              "p.m.uia_off", "D.ui||{}"):
        assert s in html, s
    assert "D.pages.map" not in html
