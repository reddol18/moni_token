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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    raw = sys.stdin.buffer.read()
    obs = None
    try:
        d = json.loads(raw or b"{}")
        obs = extract(d)
        now = int(time.time() * 1000)
        record(obs, now)
        # diagnostics: was the command invoked, and did Claude Code send rate limits? (booleans only)
        rl = d.get("rate_limits") or {}
        (config.data_dir() / "statusline.seen").write_text(json.dumps(dict(
            ts_ms=now, has_rate_limits="rate_limits" in d, has_five_hour="five_hour" in rl,
            has_seven_day="seven_day" in rl)), encoding="utf-8")
    except Exception:  # a status line must never break Claude Code
        pass
    if len(argv) >= 2 and argv[0] == "--wrap":
        try:
            r = subprocess.run(argv[1], input=raw, capture_output=True, shell=True, timeout=5)
            sys.stdout.buffer.write(r.stdout)
            return 0
        except Exception:
            pass
    # Claude Code reads UTF-8; print() would use the Windows console code page (cp949 here) and garble "·"
    sys.stdout.buffer.write(((own_line(obs) if obs else "moni_token") + "\n").encode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
