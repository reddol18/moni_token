# moni_token

Explains **why** Claude Code usage spiked — which session, which pattern (long context × many calls, cache expiry after idle, parallel sessions, …) — from local logs only. No LLM calls, no network, no message text stored.

Status: in development (M1: incremental collector). Full README (EN/KO) at release.

```
uv run moni-token collect      # read new log lines into ~/.moni_token/usage.db
uv run moni-token daily        # token totals per local date
uv run moni-token status       # current 5-hour window, speed, projection
uv run moni-token spikes       # detected spikes with evidence numbers
uv run moni-token check        # collect + desktop notification for new spikes
```

## Limitations

- **Output tokens may be undercounted.** Claude Code logs can record a partial `output_tokens` value instead of the final count. moni_token takes the maximum across the lines of one response, but the true value can still be higher. See https://github.com/anthropics/claude-code/issues/22671.
- Usage units are list-price USD equivalents from a bundled table (`src/moni_token/pricing.toml`). Prices marked `verified = false` are estimates.
- Only this PC's Claude Code logs are covered. claude.ai web chats, cloud sessions and other PCs share the same account limit but are not visible here.
