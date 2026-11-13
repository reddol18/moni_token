"""Issue #12: message catalogs, language choice, and every surface rendering in English and Korean."""
import json
import re
import string
import time
from pathlib import Path

import pytest

from moni_token import accpage, accuracy, i18n, mapserver, statusline
from moni_token.analysis import localize_cause
from moni_token.collector import collect
from moni_token.events import list_events, record_spike
from moni_token.report import build_data, render
from moni_token.settings import SpikeParams
from moni_token.spikes import detect

from test_m2 import iso

H, M = 3_600_000, 60_000
HANGUL = re.compile(r"[가-힣]")
SRC = Path(__file__).resolve().parents[1] / "src" / "moni_token"


def _fields(s: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(s) if f is not None}


def _hangul(text: str) -> list[str]:
    """Hangul left after removing the language toggle's own label (the native name of the other language)."""
    text = text.replace(i18n.NATIVE["ko"], "")
    return sorted({text[max(0, m.start() - 30):m.end() + 30] for m in HANGUL.finditer(text)})[:5]


def test_catalogs_have_the_same_keys_and_placeholders():
    langs = i18n.available()
    assert {"en", "ko"} <= set(langs)
    base = i18n.catalog("en")
    for lang in langs:
        cat = i18n.catalog(lang)
        assert set(cat) == set(base), (lang, set(cat) ^ set(base))
        for k, v in cat.items():
            assert _fields(v) == _fields(base[k]), (lang, k)
            if k.startswith("p."):   # page strings are filled by the page's T(): plain {name} only
                assert not re.search(r"\{[^}]*[:!][^}]*\}", v), (lang, k)
    assert not any(HANGUL.search(v) for v in base.values())


def test_no_hangul_in_source_outside_the_catalogs():
    bad = []
    for f in SRC.rglob("*.py"):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if HANGUL.search(line.replace(i18n.NATIVE["ko"], "")):
                bad.append(f"{f.name}:{n}")
    assert not bad, bad


def test_resolve_lang_order(tmp_path):
    cfg = tmp_path / "config.toml"
    none = tmp_path / "missing.toml"
    assert i18n.resolve_lang("en", cfg, "ko_KR") == "en"            # 1. explicit
    cfg.write_text('lang = "en"\n[spike]\nfloor_usd = 2.0\n', encoding="utf-8")
    assert i18n.resolve_lang(None, cfg, "ko_KR") == "en"            # 2. config.toml
    assert i18n.resolve_lang("ko", cfg, "en_US") == "ko"
    assert i18n.resolve_lang(None, none, "ko_KR.UTF-8") == "ko"     # 3. OS locale
    assert i18n.resolve_lang(None, none, "Korean_Korea") == "ko"
    assert i18n.resolve_lang(None, none, "de_DE") == "en"           # 4. English
    assert i18n.resolve_lang("xx", none, "ko-KR") == "ko"           # an unknown choice falls through
    assert i18n.normalize("C") is None and i18n.normalize("") is None


def test_t_falls_back_to_english_then_the_key():
    assert i18n.t("p.nav.map", "xx") == "Agent map"
    assert i18n.t("no.such.key", "ko") == "no.such.key"
    assert i18n.t("analysis.headline", "en", label="X", share=0.25) == "X (25% of the window)"


@pytest.fixture
def scenario(logs, con, tmp_path, monkeypatch):
    """A big session resumed after 3 hours (cache rewrite) followed by many long-context calls, stored in Korean."""
    monkeypatch.setenv("MONI_TOKEN_HOME", str(tmp_path / "home"))
    now = int(time.time() * 1000) // M * M
    t0 = now - 5 * H
    calls = [logs.assistant("m1", iso(t0), cr=480_000, tools=["Read"]),
             logs.assistant("m2", iso(t0 + 3 * H), c1=458_116, tools=["Bash"])]
    calls += [logs.assistant(f"m{i}", iso(t0 + 3 * H + i * 30_000), cr=460_000, tools=["Grep"]) for i in range(3, 30)]
    logs.write(logs.path(project="E--shop"), *calls)
    collect(con, logs.root)
    for s in detect(con, t0, now, SpikeParams(floor_usd=100)):
        record_spike(con, s, now, lang="ko")
    assert list_events(con)
    return con, now


def test_pages_in_english_have_no_hangul(scenario):
    con, now = scenario
    html = render(build_data(con, now, lang="en"), map_base="", lang="en")
    assert '<html lang="en">' in html and "Resumed after a break" in html and not _hangul(html), _hangul(html)
    assert "Map at that time" in html and "&lang=${LANG}" in html
    assert not _hangul(mapserver.page("en")), _hangul(mapserver.page("en"))
    acc = accpage.render(dict(accuracy.build(con, now), header={}), "en")
    assert "Forecast accuracy" in acc and not _hangul(acc), _hangul(acc)


def test_pages_in_korean_still_render(scenario):
    con, now = scenario
    html = render(build_data(con, now, lang="ko"), map_base="", lang="ko")
    assert '<html lang="ko">' in html and "공백 후 재개(캐시 만료)" in html and "그때 지도" in html
    assert "에이전트 지도" in mapserver.page("ko")
    assert "예측 정확도" in accpage.render(dict(accuracy.build(con, now), header={}), "ko")


def test_shell_has_language_toggle_and_links_keep_lang():
    html = mapserver.page("en")
    assert 'onclick="toggleLang()"' in html and ">한국어</button>" in html
    assert 'href="/usage?lang=en"' in html and 'href="/accuracy?lang=en"' in html
    assert "localStorage.setItem(kl,n)" in html and "q.set('lang',LANG)" in html
    assert 'onclick="toggleLang()"' not in render(_empty_usage())   # the static file has a fixed language


def test_server_passes_lang_through(logs, tmp_path):
    import threading
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer((mapserver.HOST, 0), mapserver.make_handler(tmp_path / "m.db", logs.root, "ko"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path):
            c = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request("GET", path)
            return c.getresponse().read().decode("utf-8")
        for path in ("/?lang=en", "/usage?lang=en", "/accuracy?lang=en&src=backtest"):
            body = get(path)
            assert '<html lang="en">' in body and not _hangul(body), (path, _hangul(body))
        assert '<html lang="ko">' in get("/usage")                 # the server's default
        assert "header" in json.loads(get("/api/state?lang=en"))
    finally:
        srv.shutdown()
        srv.server_close()


def _empty_usage():
    return dict(generated=0, start=0, end=1, five_hour={}, seven_day={}, events=[], statusline=None,
                digest=dict(headline="", items=[]), chunks=dict(overall=dict(chunks=0, token_share=0, cost_share=0),
                                                                 agents=[], min_tokens=5000), header={})


def test_stored_events_re_render_and_old_rows_fall_back():
    new = dict(code="idle_resume", label="공백 후 재개(캐시 만료)", fact_text="옛 문장", params=dict(
        idle=180.0, write=458_116, wshare=0.5, ttl="1h"), alt_key="save.cache_miss", alt_params=dict(write=10))
    c = localize_cause(new, "en")
    assert c["label"].startswith("Resumed") and "458,116" in c["fact_text"] and "1 hour" in c["interpretation"]
    assert c["alternative"].startswith("Had the cache stayed valid")
    old = dict(code="idle_resume", label="공백 후 재개(캐시 만료)", fact_text="180분 쉰 뒤 ...", facts=dict(ttl="1h"),
               interpretation="...", advice="...", alternative="새 세션으로 시작했다면 ...")
    c = localize_cause(old, "en")
    assert c["label"].startswith("Resumed") and "1 hour" in c["interpretation"]
    assert c["fact_text"] == old["fact_text"] and c["alternative"] == old["alternative"]   # numbers: stored text
    assert localize_cause(old, "ko")["interpretation"] == "캐시 TTL(1시간)이 지난 큰 대화를 이어 쓰면 대화 전체를 다시 캐시에 쓴다. (추정)"
    assert localize_cause(dict(code="parallel", alternative="x"), "en")["alternative"].startswith("The total work")


def test_rows_stored_before_issue_12_are_recovered_from_their_korean_text(scenario):
    """Old rows have only the Korean sentences: matching them against the Korean templates recovers the numbers."""
    con, _ = scenario
    causes = [c for e in list_events(con) for c in e["causes"]]
    assert {"idle_resume", "long_context"} <= {c["code"] for c in causes}
    for c in causes:
        old = {k: v for k, v in c.items() if k not in ("params", "alt_key", "alt_params")}
        ko, en = localize_cause(old, "ko"), localize_cause(old, "en")
        for k in ("label", "fact_text", "interpretation", "advice", "alternative"):
            assert ko[k] == c[k], (c["code"], k)                   # Korean: identical to what was stored
            assert not HANGUL.search(en[k] or ""), (c["code"], k, en[k])
    assert i18n.unformat("{a:,}개 {b:.0%} {a:,}", "1,234개 50% 1,234") == dict(a=1234, b=0.5)
    assert i18n.unformat("{a:,}개", "다른 문장") is None


def test_notification_in_english(logs, con):
    from moni_token.alerts import check_and_notify
    T0 = 1_791_244_800_000
    logs.write(logs.path(), logs.assistant("m1", iso(T0), cr=480_000), logs.assistant("m2", iso(T0 + 3 * H), c1=458_116))
    collect(con, logs.root)
    got = []
    check_and_notify(con, T0 + 3 * H + 2 * M, SpikeParams(floor_usd=100), lambda t, b: got.append((t, b)) or True,
                     lang="en")
    (title, body), = got
    assert title == "Claude usage: cache rewrite" and "458,116" in body and "180 min" in body
    assert not HANGUL.search(title + body)


def test_status_line_warning_follows_the_language(tmp_path, monkeypatch):
    monkeypatch.setenv("MONI_TOKEN_HOME", str(tmp_path))
    now = int(time.time() * 1000)
    w = dict(text="⚠ +4.0%p · 공백 후 재개(캐시 만료)", until_ms=now + 60_000, kind="pct", rise_pp=4.0, code="idle_resume")
    (tmp_path / "spike_now.json").write_text(json.dumps(w, ensure_ascii=False), encoding="utf-8")
    assert statusline.warning(now, "en") == "⚠ +4.0%p · Resumed after a break (cache expired)"
    assert statusline.warning(now, "ko") == "⚠ +4.0%p · 공백 후 재개(캐시 만료)"
    w.update(kind="cache_write", rise_pp=None, code=None)
    (tmp_path / "spike_now.json").write_text(json.dumps(w), encoding="utf-8")
    assert statusline.warning(now, "en") == "⚠ cache rewrite"


def test_cli_in_english(scenario, logs, tmp_path, capsys):
    from moni_token.cli import main
    db = str(tmp_path / "usage.db")   # the `con` fixture's database
    for cmd in (["events"], ["chunks", "--min-tokens", "1000"], ["savings"]):
        assert main(["--db", db, "--projects", str(logs.root), "--lang", "en", *cmd]) == 0
        out = capsys.readouterr().out
        assert out.strip() and not HANGUL.search(out), (cmd, out)
    assert main(["--db", db, "--projects", str(logs.root), "--lang", "en", "events"]) == 0
    assert "Saving:" in capsys.readouterr().out
    assert main(["--db", db, "--projects", str(logs.root), "--lang", "ko", "events"]) == 0
    assert "절약:" in capsys.readouterr().out
