"""Build a demo report from synthetic logs (for the README screenshot). No real data is read.

    uv run python scripts/demo_report.py OUT_DIR [--lang en|ko]

Writes OUT_DIR/projects/** (fake Claude Code logs), OUT_DIR/home/usage.db and OUT_DIR/report.html.
The scenario contains the patterns moni_token explains: a long conversation with many tool calls,
a large session resumed after a long idle gap (cache rewrite), parallel sessions, and big tool results.
The report language is --lang, else the MONI_TOKEN_LANG environment variable, else English.
It also writes OUT_DIR/work/shop-site (a tiny fake site) worked on by a main session and a subagent just before the
demo's "now", for the agent map screenshot: run the map with MONI_TOKEN_HOME / MONI_TOKEN_PROJECTS set to
OUT_DIR/home and OUT_DIR/projects and open the printed ?at= link.
"""
import argparse
import json
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ap = argparse.ArgumentParser()
_ap.add_argument("out", nargs="?", default="demo")
_ap.add_argument("--lang", default=os.environ.get("MONI_TOKEN_LANG", "en"))
ARGS = _ap.parse_args()
OUT = Path(ARGS.out).resolve()
os.environ["MONI_TOKEN_HOME"] = str(OUT / "home")
os.environ["MONI_TOKEN_PROJECTS"] = str(OUT / "projects")

from moni_token import config  # noqa: E402
from moni_token.blocks import compute_blocks  # noqa: E402
from moni_token.calibrate import ingest_statusline, usd_between  # noqa: E402
from moni_token.collector import collect  # noqa: E402
from moni_token.db import connect  # noqa: E402
from moni_token.events import record_spike  # noqa: E402
from moni_token.i18n import resolve_lang  # noqa: E402
from moni_token.report import write_report  # noqa: E402
from moni_token.settings import SpikeParams  # noqa: E402
from moni_token.spikes import detect  # noqa: E402

NOW = datetime(2026, 10, 7, 18, 0)
USD_PER_PCT = 0.45         # pretend calibration: 1% of the 5-hour limit ~ $0.45 of list-price usage
rnd = random.Random(7)
seq = 0


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def uid() -> str:
    global seq
    seq += 1
    return f"{seq:08d}"


class Session:
    """One conversation. Context grows with every tool result; each call re-reads it from the cache."""

    def __init__(self, project: str, sid: str, ctx: int = 18_000, model: str = "claude-opus-5",
                 cwd: str | None = None, path: Path | None = None, agent: str | None = None):
        self.project, self.sid, self.ctx, self.model = project, sid, ctx, model
        self.cwd, self.agent = cwd or f"E:\\work\\{project}", agent
        self.path = path or OUT / "projects" / f"E--work-{project}" / f"{sid}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lines: list[dict] = []
        self.last: datetime | None = None

    def base(self, t: datetime) -> dict:
        d = {"uuid": uid(), "timestamp": iso(t), "sessionId": self.sid, "cwd": self.cwd,
             "isSidechain": self.agent is not None, "entrypoint": "cli"}
        if self.agent:
            d["agentId"] = self.agent
        return d

    def human(self, t: datetime) -> None:
        self.lines.append({**self.base(t), "type": "user", "message": {"role": "user", "content": "(demo)"}})
        self.ctx += 300

    def call(self, t: datetime, tool: str | None = None, result_tokens: int = 0, out: int = 400,
             inp: dict | None = None) -> None:
        idle = self.last is not None and (t - self.last) > timedelta(hours=1)
        new = 600 + rnd.randint(0, 400)
        cr, c1 = (0, self.ctx + new) if idle else (self.ctx, 0)
        c5 = 0 if idle else new
        tid = "toolu_" + uid()
        content = [{"type": "text", "text": "(demo)"}]
        if tool:
            content.append({"type": "tool_use", "id": tid, "name": tool, "input": inp or {}})
        self.lines.append({**self.base(t), "type": "assistant", "requestId": "req_" + uid(),
                           "message": {"id": "msg_" + uid(), "model": self.model, "role": "assistant",
                                       "content": content, "stop_reason": "tool_use" if tool else "end_turn",
                                       "usage": {"input_tokens": 3, "output_tokens": out, "cache_read_input_tokens": cr,
                                                 "cache_creation_input_tokens": c5 + c1,
                                                 "cache_creation": {"ephemeral_5m_input_tokens": c5,
                                                                    "ephemeral_1h_input_tokens": c1}}}})
        self.ctx += new + out
        self.last = t
        if tool:
            self.lines.append({**self.base(t + timedelta(seconds=2)), "type": "user", "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                             "content": [{"type": "text", "text": "x" * (result_tokens * 4)}]}]}})
            self.ctx += result_tokens

    def work(self, start: datetime, n: int, every_s: int, tools=("Read", "Bash", "Grep", "Edit"),
             big_every: int = 0, big_tokens: int = 12_000) -> datetime:
        t = start
        self.human(t)
        for i in range(n):
            t += timedelta(seconds=every_s + rnd.randint(-every_s // 3, every_s // 3))
            if i and i % 9 == 0:
                self.human(t)
            tool = rnd.choice(tools) if i < n - 1 else None
            size = big_tokens if big_every and i % big_every == big_every - 1 else rnd.randint(200, 2500)
            self.call(t, tool, size if tool else 0)
        return t

    def flush(self) -> None:
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            for r in self.lines:
                f.write(json.dumps(r) + "\n")


def scenario() -> list[Session]:
    d0 = NOW.replace(hour=0) - timedelta(days=2)       # 10-05 00:00
    ss = []
    # day 1: ordinary work
    a = Session("web-app", "a1b2c3d4-0001"); a.work(d0 + timedelta(hours=10), 40, 90); ss.append(a)
    b = Session("docs-site", "b2c3d4e5-0001"); b.work(d0 + timedelta(hours=14), 25, 120); ss.append(b)
    # day 2: a long conversation that keeps calling tools (context x calls), with large file reads
    c = Session("data-pipeline", "c3d4e5f6-0001", ctx=60_000)
    t = c.work(d0 + timedelta(days=1, hours=9), 60, 70, big_every=6, big_tokens=15_000)
    t = c.work(t + timedelta(minutes=20), 45, 25, tools=("Bash", "Read"), big_every=5, big_tokens=20_000)
    ss.append(c)
    a2 = Session("web-app", "a1b2c3d4-0002"); a2.work(d0 + timedelta(days=1, hours=15), 30, 100); ss.append(a2)
    # day 3: the big session is resumed after a 20-hour gap -> whole context written to the cache again
    c.work(d0 + timedelta(days=2, hours=10, minutes=5), 12, 60)
    # day 3 afternoon: three sessions in parallel, one of them doing web research
    for i, (proj, tools) in enumerate((("web-app", ("Read", "Edit", "Bash")),
                                       ("docs-site", ("WebSearch", "WebFetch", "Read")),
                                       ("data-pipeline", ("Bash", "Read")))):
        s = Session(proj, f"d4e5f6a7-000{i}", ctx=30_000)
        s.work(d0 + timedelta(days=2, hours=14, minutes=30 + i * 3), 50, 25, tools=tools, big_every=7)
        ss.append(s)
    e = Session("web-app", "e5f6a7b8-0001"); e.work(d0 + timedelta(days=2, hours=16, minutes=10), 30, 80); ss.append(e)
    f = Session("docs-site", "f6a7b8c9-0001"); f.work(d0 + timedelta(days=2, hours=12, minutes=0), 35, 90); ss.append(f)
    return history(d0) + ss + map_demo()


MAP_FILES = {   # a small static site on disk, so the agent map has a real tree (README screenshot)
    "index.html": "<!doctype html><title>Shop</title>\n",
    "about.html": "<!doctype html><title>About</title>\n",
    "public/checkout/order-summary.html": "<!doctype html><title>Order summary</title>\n",
    "src/cart.js": "export const count = items => items.length;\n",
    "src/api.js": "export const get = url => fetch(url);\n",
    "src/menu.js": "export const open = () => {};\n",
    "styles/site.css": "body { margin: 0 }\n",
    "tests/cart.test.js": "// demo\n",
    "docs/PLAN.md": "# Plan\n\n## Cart badge\n\n- [x] count items in `src/cart.js`\n- [ ] show the badge in `index.html`\n"
                    "- [ ] style it in `styles/site.css`\n\n## Checkout\n\n- [ ] totals in `order-summary.html`\n",
}


def map_demo() -> list[Session]:
    """A main session and a running subagent on MAP_FILES in the 30 minutes before NOW: reads, edits (one of them on
    an HTML page, for the Working UI panel), Grep scopes (search marks) and lane-only calls."""
    root = OUT / "work" / "shop-site"
    for rel, text in MAP_FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    (root / ".git").mkdir(exist_ok=True)
    cwd = str(root)
    logdir = OUT / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
    sid, aid = "m1a2b3c4-0001", "a7c1e9f2d4b60003"
    a = lambda p: {"file_path": str(root / p)}
    g = lambda p: {"path": str(root / p), "pattern": "(demo)"}
    main = Session("shop-site", sid, ctx=40_000, cwd=cwd, path=logdir / f"{sid}.jsonl")
    sub = Session("shop-site", sid, ctx=12_000, cwd=cwd, path=logdir / sid / "subagents" / f"agent-{aid}.jsonl",
                  agent=aid, model="claude-sonnet-5")
    main_steps = [("Read", a("docs/PLAN.md")), ("Grep", g("src")), ("Read", a("src/cart.js")), ("Bash", None),
                  ("Read", a("src/api.js")), ("Agent", None), ("Read", a("public/checkout/order-summary.html")),
                  ("Grep", g("styles")), ("Bash", None), ("Read", a("tests/cart.test.js"))]
    sub_steps = [("Read", a("index.html")), ("Read", a("src/cart.js")), ("Edit", a("src/cart.js")),
                 ("Grep", g("public")), ("Edit", a("index.html")), ("Read", a("styles/site.css")),
                 ("Edit", a("styles/site.css")), ("Bash", None), ("Edit", a("index.html"))]
    for s, steps, start, every in ((main, main_steps, NOW - timedelta(minutes=29), 75),
                                   (sub, sub_steps, NOW - timedelta(minutes=17), 50)):
        t = start
        s.human(t)
        for i in range(int((NOW - start).total_seconds()) // every - 1):
            t += timedelta(seconds=every + rnd.randint(-every // 4, every // 4))
            tool, inp = steps[i % len(steps)]
            s.call(t, tool, rnd.randint(300, 4000), inp=inp)
    meta = sub.path.with_name(f"agent-{aid}.meta.json")
    desc = "홈 화면에 장바구니 배지 표시" if ARGS.lang.lower().startswith("ko") else "Show the cart badge on the home page"
    meta.write_text(json.dumps({"agentType": "general-purpose", "description": desc}), encoding="utf-8")
    return [main, sub]


def history(d0: datetime, days: int = 42) -> list[Session]:
    """Six earlier weeks of ordinary work, so the charts have a usual curve and a last week to compare with."""
    ss = []
    for k in range(days, 0, -1):
        day = d0 - timedelta(days=k)
        if day.weekday() >= 5 and rnd.random() < 0.7:     # mostly quiet weekends
            continue
        for j in range(rnd.choice((1, 1, 2))):
            proj = rnd.choice(("web-app", "docs-site", "data-pipeline"))
            s = Session(proj, f"h{k:02d}{j}-0000", ctx=rnd.randint(40_000, 80_000))
            s.work(day + timedelta(hours=9 + j * 5, minutes=rnd.randint(0, 50)), rnd.randint(40, 90), rnd.randint(40, 80),
                   big_every=rnd.choice((0, 6, 8)), big_tokens=rnd.randint(8_000, 16_000))
            ss.append(s)
    return ss


def statusline(con) -> None:
    """Pretend the status line recorder ran during the last day, reporting % consistent with the logs."""
    since = ms(NOW - timedelta(days=1))
    rows = []
    week_reset = ms(NOW.replace(hour=0) + timedelta(days=4, hours=9))
    week_start = week_reset - 7 * 86_400_000
    for b in compute_blocks(con, since - 5 * 3_600_000):
        t = max(b.start_ms, since)
        while t < min(b.end_ms, ms(NOW)):
            fh = usd_between(con, b.start_ms, t) / USD_PER_PCT
            sd = usd_between(con, week_start, t) / (USD_PER_PCT * 9)
            rows.append(dict(ts_ms=t, fh_pct=round(min(fh, 100)), fh_reset=b.end_ms,
                             sd_pct=round(sd), sd_reset=week_reset))
            t += 5 * 60_000
    path = config.data_dir() / "statusline.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (config.data_dir() / "statusline.seen").write_text(json.dumps(dict(ts_ms=ms(NOW), has_five_hour=True)),
                                                       encoding="utf-8")
    ingest_statusline(con, path)


def main() -> None:
    for s in scenario():
        s.flush()
    con = connect(config.db_path())
    collect(con, config.claude_projects_dir())
    statusline(con)
    lang = resolve_lang(ARGS.lang)
    for s in detect(con, ms(NOW - timedelta(days=3)), ms(NOW), SpikeParams()):
        record_spike(con, s, ms(NOW), lang=lang)
    print(write_report(con, OUT / "report.html", now_ms=ms(NOW), days=3, lang=lang))
    print(f"agent map: /?at={ms(NOW)}&paused=1&window=30&project=shop-site")


if __name__ == "__main__":
    main()
