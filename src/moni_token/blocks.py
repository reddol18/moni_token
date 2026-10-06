"""5-hour usage windows (ADR-0002 §2) and current speed/projection.

Boundary priority: a known reset time (statusline > log quotaLimits > manual) beats the ccusage-style
heuristic (first activity floored to the UTC hour). Heuristic blocks are flagged `estimated`.
"""
import sqlite3
from dataclasses import dataclass

HOUR = 3_600_000
WINDOW = 5 * HOUR
SOURCE_RANK = {"statusline": 0, "log": 1, "manual": 2}


@dataclass
class Block:
    start_ms: int
    end_ms: int
    source: str          # statusline | log | manual | estimated
    usd: float = 0.0
    calls: int = 0
    last_ms: int = 0


def known_resets(con: sqlite3.Connection) -> list[tuple[int, str]]:
    best: dict[int, str] = {}
    for r, src in con.execute("SELECT resets_at_ms, source FROM limit_obs WHERE kind IN ('five_hour','unknown')"):
        r = r - r % 60_000  # same reset reported with second jitter
        if r not in best or SOURCE_RANK.get(src, 9) < SOURCE_RANK.get(best[r], 9):
            best[r] = src
    return sorted(best.items())


def compute_blocks(con: sqlite3.Connection, since_ms: int = 0) -> list[Block]:
    resets = known_resets(con)
    blocks: list[Block] = []
    cur: Block | None = None
    for ts, usd, n in con.execute("SELECT t5_ms, SUM(usd), SUM(calls) FROM buckets WHERE t5_ms >= ? "
                                  "GROUP BY t5_ms ORDER BY t5_ms", (since_ms,)):
        if cur is None or ts >= cur.end_ms:
            anchor = next(((r, s) for r, s in resets if r - WINDOW <= ts < r), None)
            if anchor:
                cur = Block(anchor[0] - WINDOW, anchor[0], anchor[1])
            else:
                start = ts - ts % HOUR
                if blocks and start < blocks[-1].end_ms:   # never overlap the previous window
                    start = blocks[-1].end_ms
                cur = Block(start, start + WINDOW, "estimated")
            blocks.append(cur)
        cur.usd += usd
        cur.calls += n
        cur.last_ms = ts
    return blocks


def window_usd(con: sqlite3.Connection, start_ms: int, end_ms: int) -> float:
    (v,) = con.execute("SELECT COALESCE(SUM(usd),0) FROM buckets WHERE t5_ms >= ? AND t5_ms < ?",
                       (start_ms, end_ms)).fetchone()
    return v


def status(con: sqlite3.Connection, now_ms: int, rate_window_min: int = 15) -> dict:
    """Current block, speed over the last `rate_window_min`, linear projection to block end."""
    blocks = compute_blocks(con, now_ms - WINDOW - HOUR)
    cur = next((b for b in reversed(blocks) if b.start_ms <= now_ms < b.end_ms), None)
    rate = window_usd(con, now_ms - rate_window_min * 60_000, now_ms) / rate_window_min
    out = dict(now_ms=now_ms, rate_usd_per_min=rate, block=None)
    if cur:
        remaining = (cur.end_ms - now_ms) / 60_000
        out["block"] = dict(start_ms=cur.start_ms, end_ms=cur.end_ms, source=cur.source, usd=cur.usd,
                            calls=cur.calls, projected_usd=cur.usd + rate * remaining)
    return out
