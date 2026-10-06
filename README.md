# moni_token

Explains **why** Claude Code usage spiked — which session, which pattern (long context × many calls, cache expiry after idle, parallel sessions, …) — from local logs only. No LLM calls, no network, no message text stored.

Status: in development (M1: incremental collector). Full README (EN/KO) at release.

```
uv run moni-token collect      # read new log lines into ~/.moni_token/usage.db
uv run moni-token daily        # token totals per local date
```
