# moni_token

**English** · [한국어](README.ko.md)

**moni_token is a local usage monitor for Claude Code that never calls an LLM, so it spends zero tokens.** It reads
Claude Code's own logs on your PC. It shows your 5-hour and weekly limit % against a baseline of your usual range, and
it explains every spike with rule-based causes (which session, which pattern, how much you could have saved). A live
agent map shows where the main session and its subagents are working in your source tree. Each running subagent gets
a progress forecast, and the tool scores how accurate those forecasts turn out to be. Nothing leaves your machine.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) (more under [Install](#install)).

```powershell
git clone https://github.com/reddol18/moni_token.git; cd moni_token; uv sync
uv run moni-token collect     # read existing Claude Code logs into ~/.moni_token/usage.db
uv run moni-token map         # usage page, agent map and forecast accuracy at http://127.0.0.1:4990/
```

The UI is in English or Korean. Pick one with `?lang=en|ko` on a page, `--lang` on the CLI or `lang = "en"` in
`~/.moni_token/config.toml`; otherwise the OS locale decides. The page header also has a toggle
(see [Adding a language](#adding-a-language)).

---

> "I only did a little work, so why did 5% of my Claude limit disappear in 50 minutes?"

moni_token reads Claude Code's local logs and answers that question. When your limit usage jumps, it tells you
**which session** caused it, **which pattern** was behind it (a long conversation calling tools over and over, a big
session resumed after a long break, several sessions running in parallel, …) and **how much you could have saved**
by doing it differently.

It does three things:

1. **Explains spikes** — a static dashboard of limit % over time with every spike, its cause and the saving
   (refreshed every 2 minutes, with a desktop notification).
2. **Warns while it happens** — a short `⚠ +4.0%p · cause` segment on Claude Code's status line, also in front of
   [claude-hud](https://github.com/jarrodwatts/claude-hud) if you use it.
3. **Shows where agents are working** — a live map of the main session and its subagents on the project's real
   source tree, UI pages and the features of a contract document.

Principles:

- **No LLM calls.** A tool meant to save tokens should not spend them. Every judgement is a fixed rule, and every
  sentence comes from a template.
- **Nothing leaves your PC.** No network access. The dashboard is a static HTML file; the agent map is served on
  127.0.0.1 only.
- **No conversation text is stored.** Only numbers (tokens, times, call counts) and metadata (session id, project
  folder name, model, tool names, sizes, and the project-relative path a tool worked on — [ADR-0004](docs/adr/0004-agent-map-paths-and-server.md)).
- **Logs are read-only.** It never changes or deletes Claude Code's files.

![moni_token dashboard](docs/img/dashboard.png)

<sub>Screenshot from synthetic demo data (`scripts/demo_report.py --lang en`).
Dark theme: [docs/img/dashboard-dark.png](docs/img/dashboard-dark.png) — it follows the OS setting, or the ◐ button.</sub>

Charts are drawn with [uPlot](https://github.com/leeoniya/uPlot), bundled in the repo: drag to zoom, double-click to
reset, the cursor is shared between the 5-hour and weekly charts, and clicking a red spike band opens that spike.

## What it shows

| Section | What you learn |
|---|---|
| Tiles at the top | Current 5-hour and weekly limit usage in %, when they reset, and spikes in the last 24 hours |
| Savings summary | How many %p you could have saved this week, per cause, with the alternative (e.g. "start a new session") |
| Large chunks | How often raw tool output (whole files, long command output) was sent instead of a conclusion, and what carrying it in the context cost, per project |
| Limit charts | Limit % over time. Solid line = measured (from Claude Code's status line), dashed line = estimated from the logs. Red bands are spikes |
| Spike list | Each spike with its cause. Click a row for the per-project share, the evidence numbers and a timeline of the calls |

![Spike detail](docs/img/event-detail.png)

### Causes it recognises

| Cause | How it is detected | Typical advice |
|---|---|---|
| Long conversation × many calls | Each call re-reads a large cached context, and there are many calls in the window | `/compact`, or continue in a new session; bundle repetitive steps into a script |
| Resumed after idle (cache expired) | The first call after a gap longer than the cache TTL writes the whole context to the cache again | Don't resume a large session after a long break; start a new one |
| Parallel sessions / subagents | Several sessions or sidechains are active in the same window | Run fewer at once, or one after another |
| Cache miss | Cache writes surge in the middle of a session | Avoid switching model or settings mid-task |
| Large input | Context jumps right after reading an image or a big file | Read only the part you need; pass raw content as a file |
| Web research | Many WebSearch / WebFetch calls | Narrow the research and keep only summaries |
| Output burst | Output tokens take an unusual share | Have long results written to a file; limit output length |

The cause and its numbers are facts from the logs; the interpretation is labelled as an estimate.

## How it differs from existing tools

**Usage and cost tools**

| Tool | What it does well | What moni_token adds |
|---|---|---|
| [ccusage](https://github.com/ryoppippi/ccusage) | Token and cost totals per day, session and 5-hour block | Explains *why* a period was expensive and records it as an incident |
| [Claude Code Usage Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) | Live terminal dashboard, burn rate, limit predictions | Attributes a spike to a session and a pattern, with a counterfactual saving |
| ccflare | Local web UI with token, cost and speed charts | Incident list with causes and per-call timelines |
| [claude-hud](https://github.com/jarrodwatts/claude-hud) | Status line: context, limits, active tools, running agents, todos | Spike warning with its cause in front of claude-hud's line (moni_token wraps it rather than re-implementing it) |
| [agents-observe](https://github.com/simple10/agents-observe) | Hook events into a local web UI: subagent tree, timeline, per-agent tokens | Positions on the source tree / UI pages / contract features; reads past sessions from the logs; no Docker, no prompt or file text stored |

**Progress and time-left tools** (as described on each tool's own page, checked 2026-11-13; "not stated" means the
page does not say, not "no")

| Tool | Shows | How progress / time is estimated | Model calls | Tracks own accuracy | 5-hour / weekly limit | Runs as |
|---|---|---|---|---|---|---|
| Claude Code built-in subagent progress summaries | Short status text on each running subagent's row | Written by a model from the subagent's work | Yes: the changelog calls them "requests"; cost is reported by users, not documented | No | No | Built in; off with `CLAUDE_CODE_DISABLE_AGENT_PROGRESS_SUMMARIES=1` |
| [claude-time-left](https://github.com/DJsluxx/claude-time-left) | Subagents, long tool calls, current turn: elapsed, time left, bar | Median of the last 20 runs of the same kind (`agent:Explore`, `tool:Bash`); built-in default marked `(guess)` | Not stated | No (shows "over by …") | Not mentioned | Plugin pane beside the chat |
| [claude-time-estimate](https://github.com/TomerBenShimol/claude-time-estimate) | Countdown above the prompt; background agents with `sub-agents 2/5` | Task list: average time per completed task × open tasks; else median of past responses longer than the current one (≥5 needed) | None described; no network calls | No ("a rough guide") | Not mentioned | Mod band above the prompt |
| [claude-code-progress-board](https://github.com/ewxgwy1987/claude-code-progress-board) | Goal, tasks, subagents, workflows, tool activity, each with bar and time left | Median of the last 30 durations of the same kind; % = elapsed / estimate, capped until done | Timers: none. One small Haiku call (≤120 output tokens) to name an untitled workflow agent | No (flags "over estimate") | Not mentioned (points to a separate meter) | Mod pane (terminal and desktop) |
| [progress-hud](https://github.com/jandro-es/progress-hud) | Subagents, tasks, shells, totals: ETA, cost, burn rate, context, rate-limit windows | Durations learned across sessions; time-based bars; task done/total | Not stated; cost priced from token counts | Not stated | Shows rate-limit windows | Mod pane + one-line status line |
| [task-progress-bar](https://github.com/PRAFULREDDYM/task-progress-bar) | Task completion %, done / in progress / pending, time left | Task files; EMA (α 0.3) of intervals between completions × tasks left | None ("no tokens consumed") | No | No | Status line + `PostToolUse` hook, also CLI |
| [claude-code-agent-status](https://github.com/hg913488/claude-code-agent-status) | Main thread + up to 12 subagents: current action, progress, model, elapsed | Task list done/total; otherwise a step-based estimate capped at 90% | Not stated; "nothing is added to Claude's prompt" | Not stated | Not mentioned | Mod panel (`/agent-status`) |
| [savvy-progress](https://claude-mods.com/savvy-progress/) | Every subagent: task, model, effort, context fill, cost, elapsed | Counts only: planned tasks (with savvy-flow) and steps the worker reports; no forecast | No model calls; adds a short tool description to the prompt | No | No | Mod panel + bar above the prompt |
| **moni_token** | Limit % with baselines, spike causes, agent map, per-file token cost | Subagents compared with ≥10 finished runs of the same `agentType` that got as far: P50/P85 for calls, time and remaining 5-hour %p | None | Yes: `/accuracy` page and `moni-token forecast` replay | Yes: both, with baselines | Local web page + status-line segment |

- **Built-in summaries.** Officially, the [changelog](https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md)
  (2.1.323) says `CLAUDE_CODE_DISABLE_AGENT_PROGRESS_SUMMARIES=1` turns off "subagent progress summaries and the
  requests that write them"; the variable has existed since 2.1.318. As of 2026-11-13 the
  [env-var docs page](https://code.claude.com/docs/en/env-vars) does not list it. The cost is *reported*, not
  documented: [one test](https://bestagent.dev/claude-code-background-agent-labels-cost-2026) measured 0.1–0.2% of
  session cost with Haiku writing the labels, and about 19% when telemetry was off and the whole agent context went to
  the main model; other reports range up to 15–20%. moni_token does not replace these labels; it records what you
  spend, with or without them.
- **Where the others are better.** Most of these tools live inside Claude Code as a pane, band or status line, so you
  see progress without leaving the terminal. moni_token's map and forecasts are on a local web page; only the spike
  warning is on the status line. claude-time-estimate, task-progress-bar and claude-code-agent-status use Claude's own
  task list (`TaskCreate` / `TodoWrite`) for progress. moni_token does not; it reads checklist ticks only from
  contract documents. progress-hud also tracks shells and per-agent cost, and claude-time-left and progress-board also
  time tool calls and the main turn.
- **Where moni_token differs.** It forecasts subagents only (the main agent gets none) and works in calls, minutes and
  5-hour %p, not just minutes. It conditions on how far a run has already got. It is the only tool here that scores
  its own forecasts, on an `/accuracy` page. None of the progress tools above ties a forecast to the 5-hour or weekly
  limit. progress-hud shows the rate-limit windows, but it does not forecast against them.
- **Cold start.** moni_token needs 10 comparable past runs before it shows a forecast. claude-time-estimate needs 1
  run for agents and 5 for turns, and claude-time-left and progress-board start from a default or show "no estimate
  yet".
- **Token use.** moni_token, task-progress-bar and claude-time-estimate describe no model calls. progress-board makes
  one small Haiku call in one case, and savvy-progress adds a short tool description to the prompt.
  claude-time-left, progress-hud and claude-code-agent-status do not say either way.
- **Licences.** Every tool here is MIT except claude-time-estimate, whose page states no licence.

moni_token does not try to replace these tools. Use ccusage for totals and claude-hud for the live HUD; use moni_token
when a number surprises you or you want to see where your agents are.

<details>
<summary>Sources (checked 2026-11-13)</summary>

Official (Anthropic):
- [Claude Code changelog](https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md): the 2.1.318,
  2.1.321 and 2.1.323 entries on progress summaries
- [Environment variables docs](https://code.claude.com/docs/en/env-vars): `CLAUDE_CODE_DISABLE_AGENT_PROGRESS_SUMMARIES` is not listed
- [Subagents docs](https://code.claude.com/docs/en/sub-agents): no section on progress summaries

Reported by the community (not official):
- [bestagent.dev](https://bestagent.dev/claude-code-background-agent-labels-cost-2026): one-machine measurement,
  0.1–0.2% with Haiku labels, ~19% with telemetry off
- [wmedia.es](https://wmedia.es/en/tips/claude-code-background-agents-status-cost): one-session `--debug-file`
  measurement (~50k input tokens for 14 output tokens on the old path)
- [flamerix-ai gist](https://gist.github.com/flamerix-ai/c3905705cc4205e5de3011fa2e74efce): 16.4% of one team's
  spend, as cited by bestagent.dev
- [Hacker News thread](https://news.ycombinator.com/item?id=50284255): a reported reply from an Anthropic engineer,
  as cited by bestagent.dev

Tools: each tool's own README or page, linked in the table above. The usage and cost rows were written earlier and
were not re-checked on 2026-11-13.

</details>

## Install

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/). Developed and used on Windows 10/11. The code has
macOS (`osascript`) and Linux (`notify-send`) notification paths, but they are untested; the setup commands below are
for Windows.

```powershell
git clone https://github.com/reddol18/moni_token.git
cd moni_token
uv sync
uv run moni-token collect     # first run reads all existing logs into ~/.moni_token/usage.db
uv run moni-token report      # writes ~/.moni_token/report.html
start $HOME\.moni_token\report.html
```

That's it for a one-off look. The steps below make it run on its own.

### 1. Refresh every 2 minutes (recommended)

`check` collects new log lines, shows a desktop notification for a new spike and rewrites the report. It takes
about a second. On Windows, register it with Task Scheduler:

```powershell
uv run moni-token install-task                 # every 2 minutes, also on battery
uv run moni-token install-task --no-battery    # pause while on battery
uv run moni-token install-task --map           # also start the agent map server at logon (and now)
uv run moni-token uninstall-task               # remove both
```

It runs only while you are logged on. Running `install-task` again replaces the task, so you can switch the battery
option at any time.

### 2. Status line recorder (recommended)

Claude Code passes the real limit % to the status line command. moni_token records just those numbers, which gives
the solid "measured" line and calibrates the estimate automatically. Add this to `~/.claude/settings.json`:

```json
"statusLine": {
  "type": "command",
  "command": "C:/path/to/moni_token/.venv/Scripts/moni-token-statusline.exe"
}
```

Use forward slashes. On Windows, Claude Code runs this command through Git Bash, which drops backslashes, so a
`C:\\path\\...` command silently never runs.

The status line then shows something like `5h 12% ~15:06 · ctx 40%`. If you already have a status line command,
keep it and wrap it: `moni-token-statusline.exe --wrap "<your old command>"`.
It starts recording from the next new Claude Code session.

While a spike is in progress, the status line starts with a short warning such as `⚠ +4.0%p · Resumed after idle`
(it comes from the last `check` run and disappears after `realert_min`).

**With [claude-hud](https://github.com/jarrodwatts/claude-hud).** moni_token doesn't try to be a full HUD. If you use
claude-hud, keep moni_token as the status line command and wrap claude-hud's command, so the limit numbers are still
recorded and the warning appears in front of claude-hud's line:

```json
"statusLine": {
  "type": "command",
  "command": "C:/path/to/moni_token/.venv/Scripts/moni-token-statusline.exe --wrap \"<claude-hud's command>\""
}
```

claude-hud's `/hud` setup writes its own command into `statusLine`; copy that command into `--wrap` afterwards.
On Windows with Git Bash that is `--wrap "C:\Users\<you>\.claude\plugins\claude-hud\statusline.cmd"` (keep the
quotes; backslashes are fine inside them).

### 3. Without the status line: enter `/usage` by hand

Type `/usage` in Claude Code and copy the numbers:

```powershell
uv run moni-token observe --session 49 --session-reset "2026-10-07 02:30" --week 7 --week-reset "2026-10-13 18:00"
```

One reading is enough to draw the estimate. More readings make it more accurate.

## Live agent map

```powershell
uv run moni-token map          # http://127.0.0.1:4990/
```

![Live agent map](docs/img/agent-map.png)

<sub>This repository's own map during development:
- a running subagent (general-purpose) with its progress forecast;
- the main agent marked "Progress forecast: not for main";
- edited files in red, each touched file with its token cost.</sub>

Shows where each agent (main session and subagents) is working right now:

- **Agent timeline**: one row per agent over the selected period, one mark per tool call, coloured by kind
  (read / edit / run / web / subagent / MCP). Click a mark to jump to its file in the tree. Grid lines sit on round
  clock times (every 5 / 15 / 30 min), so they slide with the marks; a mark's tooltip gives its time to the second.
- **Pause**: the `⏸ Pause` button (or the space bar) freezes every panel at the current moment; hover, click-to-tree,
  search, period and project changes keep working on that moment, ◀ ▶ step by one minute, and `Link to this moment`
  links to it (`?at=…&project=…&window=…`). `▶ Live` returns to live. `?paused=1` opens the map already paused.
- **Source tree**: the real directory tree. Files an agent read or edited in the period are bold (red when edited),
  a bar shows how often each file or folder was touched, and a ringed dot marks where each running agent is now.
  Large folders are cut at 200 entries; touched files always show. A search box filters the tree.
  Next to each touched file: **what it cost in tokens**, e.g. `9k ×65 → 352k` — its tool result added 9k tokens to
  the context and was re-read by 65 later calls, so the conversation carried 352k tokens of it (≈ %p of the 5-hour
  limit when calibrated). Folders show the sum; each agent shows its heaviest file. Same measurement as
  `moni-token chunks`.
- **UI pages**: routes found from real files (Next.js `app/` and `pages/`, static `*.html`), highlighted when an agent
  touches their source.
- **Features**: sections of a contract document (`docs/PLAN.md`, `PLAN.md`, `SPEC.md`, `docs/tasks/*.md`), linked to
  files only through paths or unique file names written in backticks in that section; `- [x]` items give progress.

**Progress forecast** ([ADR-0005](docs/adr/0005-progress-forecast.md)): each unfinished subagent is compared with
finished runs of the same kind (`agentType`) that got at least as far, e.g. "call 4 · usually done by 21 (P50) – 46
(P85) · 3–9 min left · 193 past runs". Fewer than 10 comparable runs shows "not enough history". The agents panel
also shows how much of the 5-hour limit the running agents will probably still use (≈ +%p, P50–P85) and warns when
P85 would cross 100%. Contract documents get a completion estimate from the pace of past checklist ticks (snapshots
taken by `check`). `moni-token forecast` replays the past to check the ranges: on the author's PC, 92% of forecasts
fell within P85 and 59% within P50 — slightly wide, on the safe side.

The same server also serves the usage page at **http://127.0.0.1:4990/usage**, built from live data. Both pages share
a top line with the current 5-hour and weekly limit % and the live spike warning. Every spike on the usage page has a
**"Map at that time"** link that opens the map at the spike's end, on its top project; on the map, each agent and
project shows its share of the current 5-hour window (≈ %p via calibration, labelled as an estimate).

**Is this usage normal?**
- Both limit charts on the usage page draw a baseline behind the current line:
  - the usual curve: the P50, with a P25–P75 band, of every earlier window at the same point into the window;
  - last week's line.
- A label next to each chart title gives the verdict with its numbers, for example: "▲ high · 20% now · usually 5.9% at day 2 (P25–P75 0–14%) · 10 past windows". Below 5 past windows it says "not enough history".
- A pace line shows the % you would reach at the reset if the last hour's slope (the last day's for the weekly chart) continues, and when 100% would come first.

The main agent gets no forecast: in the recommended setup it only directs and reviews. A tooltip on the map says so.

![Forecast accuracy](docs/img/accuracy.png)

**Forecast accuracy** at **http://127.0.0.1:4990/accuracy**:
- **Live record:** the map server (every 20 s) and `check` store the forecast shown for each running subagent, once per call count. Only numbers are stored, in a `forecast_snaps` table. Each forecast is scored once the run finishes.
- **What the page shows:**
  - the P50 and P85 hit rates (targets about 50% and 85%);
  - the median error;
  - a verdict: well calibrated, too wide or too narrow;
  - these numbers per agent type, per session (small charts of the P50/P85 given at each call count against the real total) and as a weekly trend.
- **Backtest tab:** the same views for the replay that `moni-token forecast` runs.
- **Closed sessions:** runs whose session closed before they finished count as "unresolved", not as misses.

Add `?at=<epoch ms>` to the URL to look at a past moment (the tree is today's). The server listens on 127.0.0.1 only
and collects new log lines every 5 seconds. From the logs it keeps only tool names and project-relative target paths
(ADR-0004); subagent task descriptions are not shown.

## Commands

| Command | What it does |
|---|---|
| `collect` | Read new log lines (incremental; only the new part of each file) |
| `check` | `collect` + notification for new spikes + report refresh (for the scheduled task) |
| `install-task [--no-battery]` / `uninstall-task` | Register / remove the 2-minute `check` task (Windows) |
| `report [--days 7]` | Write the HTML dashboard |
| `map [--port 4990]` | Live agent map on 127.0.0.1 |
| `forecast` | Backtest the progress forecast on past subagent runs (P50/P85 coverage) |
| `status` | Current 5-hour window, speed and projection |
| `events` | Recorded spikes with their savings sentence |
| `savings` | What could have been saved this week, by cause |
| `analyze --from "2026-10-06 12:31" --to "2026-10-06 12:56"` | Explain any window you choose (`--save` records it) |
| `chunks` | Raw chunks sent to the API, per project |
| `daily` / `blocks` | Token totals per day / 5-hour windows |
| `observe` / `calibrate` | Enter limit % you saw, for calibration |
| `spikes`, `backfill-events`, `suggest-floor` | Detection details and tuning |

Run any of them as `uv run moni-token <command>`.

### Settings

Detection thresholds live in `~/.moni_token/config.toml` (all optional):

```toml
[spike]
pct_jump = 3.0          # 5-hour limit rising this many %p within window_min is a spike
window_min = 15
realert_min = 30        # no repeat notification within this many minutes
single_call_cache_write_tokens = 200000   # one call re-caching this much is a spike by itself
```

## Try it without your own data

```powershell
uv run python scripts/demo_report.py demo --lang en
start demo\report.html
```

This writes fake logs with all the patterns above and builds a report from them (`--lang ko` for Korean). The
dashboard screenshots in this README come from it.

## Limitations

- **Limit % is the account's value; the logs only cover this PC.** claude.ai web chats, cloud sessions and other PCs
  use the same limit but are invisible here, so estimates (the dashed line) can be lower than reality. Measured values
  from the status line are exact.
- **Output tokens may be undercounted.** Claude Code logs can record a partial `output_tokens` value. moni_token takes
  the maximum across the lines of one response, but the true value can still be higher. See
  https://github.com/anthropics/claude-code/issues/22671.
- Usage is weighted with a bundled list-price table (`src/moni_token/pricing.toml`). Prices marked
  `verified = false` are estimates. The table only sets the relative weight of token types; the % comes from
  calibration.
- Causes are rule-based. A window that fits no rule is shown as "no matching cause".
- The Claude Code log format is not a public API and may change between versions.

## Development

```powershell
uv run pytest
```

Design notes are in `docs/adr/`. A test checks that the package never imports an LLM client.

## Adding a language

Every user-facing string comes from a message catalog: one JSON file per language in `src/moni_token/locales/`
(`en.json`, `ko.json`). Nothing is translated at runtime.

1. Copy `en.json` to `<code>.json`, e.g. `ja.json`, and translate the values. Keep the keys and every `{placeholder}`
   exactly as they are; format specs such as `{share:.0%}` stay too. Keys that start with `p.` are page strings.
   Their placeholders are plain `{name}`, and `p.locale` is the locale used for dates and numbers, e.g. `ja-JP`.
2. Add the language's own name to `NATIVE` in `src/moni_token/i18n.py`. The language toggle shows that name.
3. Run `uv run pytest`. `tests/test_i18n.py` fails if a key or a placeholder is missing.

The language is chosen in this order: `--lang` on the CLI or `?lang=` on a page, then `lang = "<code>"` in
`~/.moni_token/config.toml`, then the OS locale, then English. The toggle in the page header switches between Korean
and English and remembers the choice in the browser. Events recorded earlier are shown in the current language too,
because each one keeps its template keys and numbers.

## License

MIT — see [LICENSE](LICENSE).

Bundled third-party code: uPlot 1.6.32, MIT, © Leon Sorokin — `src/moni_token/vendor/uplot/LICENSE`.
