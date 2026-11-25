"""Claude Code status line recorder (ADR-0002 §4).

Claude Code pipes a JSON object to the status line command on every update. We keep only numbers
(rate-limit percentages, reset times, context %) plus the session id, append them to
~/.moni_token/statusline.jsonl when they change, and print a status line.

With --wrap, the user's previous status line command runs on the same stdin and its output is shown
unchanged. That command is the only process this module starts. Must never fail or block.
"""
import json
import subprocess
import sys
import time
from datetime import datetime

from . import config


def extract(d: dict) -> dict:
    rl = d.get("rate_limits") or {}
    fh, sd = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    cw = d.get("context_window") or {}
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return dict(sid=str(d.get("session_id") or "")[:64],
                fh_pct=num(fh.get("used_percentage")), fh_reset=num(fh.get("resets_at")),
                sd_pct=num(sd.get("used_percentage")), sd_reset=num(sd.get("resets_at")),
                ctx_pct=num(cw.get("used_percentage")))


def record(obs: dict, now_ms: int) -> bool:
    """Append when the limit numbers changed. Returns True if a line was written."""
    d = config.data_dir()
    d.mkdir(parents=True, exist_ok=True)
    key = [obs["fh_pct"], obs["fh_reset"], obs["sd_pct"], obs["sd_reset"]]
    if all(v is None for v in key):
        return False
    last = d / "statusline.last"
    try:
        if json.loads(last.read_text(encoding="utf-8")) == key:
            return False
    except (OSError, ValueError):
        pass
    with open(d / "statusline.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts_ms": now_ms, **obs}) + "\n")
    last.write_text(json.dumps(key), encoding="utf-8")
    return True


def own_line(obs: dict) -> str:
    parts = []
    if obs["fh_pct"] is not None:
        reset = f" ~{datetime.fromtimestamp(obs['fh_reset']).strftime('%H:%M')}" if obs["fh_reset"] else ""
        parts.append(f"5h {obs['fh_pct']:.0f}%{reset}")
    if obs["sd_pct"] is not None:
        parts.append(f"7d {obs['sd_pct']:.0f}%")
    if obs["ctx_pct"] is not None:
        parts.append(f"ctx {obs['ctx_pct']:.0f}%")
    return " · ".join(parts) or "moni_token"


def warning(now_ms: int, lang: str | None = None) -> str:
    """The live spike warning written by `check`, or "" (issue #2). Never raises.
    When `check` stored its numbers (issue #12) it is rendered in `lang` (None: config.toml, then the OS locale);
    otherwise the stored text is shown. No database: one small JSON file, and the catalog only when needed."""
    try:
        w = json.loads(config.live_path().read_text(encoding="utf-8"))
        if not w or now_ms >= w["until_ms"]:
            return ""
        if "kind" in w:
            try:
                from .i18n import resolve_lang, warning_text
                return warning_text(w, lang or resolve_lang())
            except Exception:
                pass
        return str(w["text"])
    except Exception:
        return ""


STALE_MS = 12 * 3_600_000   # same as cloud.STALE_MS (not imported: cloud.py is heavier than this hot path needs)


def _cloud_enabled() -> bool:
    p = config.data_dir() / "config.toml"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False
    if "cloud" not in text:
        return False
    import tomllib
    try:
        c = tomllib.loads(text).get("cloud")
    except tomllib.TOMLDecodeError:
        return False
    return isinstance(c, dict) and c.get("enabled") is True


def _money(v: float, cur: str) -> str:
    return f"${v:,.2f}" if cur == "USD" else f"{v:,.2f} {cur}"


def aws_segment(now_ms: int, lang: str | None = None) -> str:
    """AWS month-to-date cost from the cache written by `moni_token map` (issue #18, ADR-0008), or "".
    Never calls AWS. Hidden when the feature is off or the last successful refresh is older than 12 hours."""
    try:
        if not _cloud_enabled():
            return ""
        st = json.loads((config.data_dir() / "cloud_cost.json").read_text(encoding="utf-8"))
        m, b = st.get("metric") or None, st.get("budget") or None
        fresh = lambda x: x and now_ms - x.get("fetched_ms", 0) <= STALE_MS
        from .i18n import resolve_lang, t
        lg = lang or resolve_lang()
        if fresh(m):
            amount, cur, ts = m["amount"], m.get("currency", "USD"), m.get("ts_ms") or m["fetched_ms"]
        elif fresh(b):
            amount, cur, ts = b["actual"], b.get("unit", "USD"), b.get("updated_ms") or b["fetched_ms"]
        else:
            errs = [e for e in (st.get("err") or {}).values() if e]
            recent = any(now_ms - e.get("at_ms", 0) <= STALE_MS for e in errs)
            return t("cloud.unknown", lg) if recent and not (m or b) else ""
        fc = (t("cloud.sl_fc", lg, amount=_money(b["forecast"], b.get("unit", "USD")))
              if fresh(b) and b.get("forecast") is not None else "")
        mins = max(0, (now_ms - ts) // 60_000)
        age = (t("cloud.age_m", lg, n=mins) if mins < 60 else t("cloud.age_h", lg, n=round(mins / 60))
               if mins < 48 * 60 else t("cloud.age_d", lg, n=round(mins / 1440)))
        return t("cloud.sl", lg, amount=_money(amount, cur), fc=fc, age=age)
    except Exception:
        return ""


def _opts(argv: list[str]) -> dict:
    out, i = {}, 0
    while i + 1 < len(argv):
        if argv[i] in ("--wrap", "--lang"):
            out[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    opts = _opts(argv)
    raw = sys.stdin.buffer.read()
    obs = None
    now = int(time.time() * 1000)
    warn = " · ".join(x for x in (warning(now, opts.get("lang")), aws_segment(now, opts.get("lang"))) if x)
    try:
        d = json.loads(raw or b"{}")
        obs = extract(d)
        record(obs, now)
        # diagnostics: was the command invoked, and did Claude Code send rate limits? (booleans only)
        rl = d.get("rate_limits") or {}
        (config.data_dir() / "statusline.seen").write_text(json.dumps(dict(
            ts_ms=now, has_rate_limits="rate_limits" in d, has_five_hour="five_hour" in rl,
            has_seven_day="seven_day" in rl)), encoding="utf-8")
    except Exception:  # a status line must never break Claude Code
        pass
    if opts.get("wrap"):
        try:
            r = subprocess.run(opts["wrap"], input=raw, capture_output=True, shell=True, timeout=5)
            # the wrapped line keeps its own bytes (and colors); the warning goes in front of its first line
            sys.stdout.buffer.write((warn + " │ ").encode("utf-8") + r.stdout if warn else r.stdout)
            return 0
        except Exception:
            pass
    line = own_line(obs) if obs else "moni_token"
    # Claude Code reads UTF-8; print() would use the Windows console code page (cp949 here) and garble "·"
    sys.stdout.buffer.write(((warn + " · " + line if warn else line) + "\n").encode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
