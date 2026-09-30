# Model gateway

One local relay, owned by this repo, between every `claude -p` the fleet runs and the Copilot proxy. Code: `scripts/model_gateway.py`. Service: `com.andyherman.neural-bridge.model-gateway`, listening on `127.0.0.1:4142`, in front of copilot-api on `:4141`.

```
agent turn / flush / compile / lint
      │  claude -p   (claude_env.proxy_env → ANTHROPIC_BASE_URL)
      ▼
model gateway :4142   fold system messages · restart the proxy on 403 · log metadata
      ▼
copilot-api :4141     Anthropic /v1/messages → GitHub Copilot
```

## What it fixes

**Claude 5 on the Copilot route.**
- Claude Code sends Claude 5 models a conversation that ends with a `role: "system"` message.
- copilot-api passes it through, and Copilot refuses it with 400: "This model does not support assistant message prefill. The conversation must end with a user message."
- From at least 2026-09-25 that meant no Claude 5 model worked for the fleet.
- The gateway moves those messages into the top-level system prompt.

Verified 2026-09-30 through the gateway with Claude Code 2.1.286: `claude-sonnet-5`, `claude-opus-5` and `claude-opus-5.5` work, including a tool call. Opus 5.5 also needs Claude Code 2.1.28x or later.

**A stale proxy token.**
- copilot-api can keep a dead Copilot token and answer every model 403 "forbidden", while logging nothing about it. On 2026-09-30 that took Luna down until the proxy was restarted by hand.
- On a 403 for `/v1/messages`, the gateway restarts copilot-api (at most once every five minutes), waits for it to answer, and retries the request once.
- If the retry is refused too, it raises a review-queue alert ("Copilot proxy refuses requests even after a restart") and clears it on the next success.

## Tool lists

Every proxy-route call now passes `--strict-mcp-config` (`claude_env.mcp_args`). A turn loads exactly the MCP servers it was granted (`scripts/discord_bot/private_tools.py`), and otherwise none.

Before this, every agent turn inherited Andy's user-level MCP servers: about 150 tool definitions per call, paid tools among them. On Claude 5 that was enough prompt for a single file read to fail with "Prompt is too long".

## When it is down

`claude_env.proxy_base()` checks the gateway with a quick TCP connect. If the gateway is not answering, calls go straight to copilot-api:
- 4.x models keep working;
- Claude 5 ids do not.

`claude_env.proxy_supports()` names that boundary. A default that must survive a gateway outage, such as the memory pipeline's, stays on 4.x.

The memory canary reads `/_gateway/health` daily and fails if:
- the gateway is unreachable;
- the proxy behind it is down;
- the restart alert is open.

## Operating it

| Task | How |
|---|---|
| Health | `curl -s localhost:4142/_gateway/health` returns requests, folded, retried, restarts, alert_open |
| Log | `~/Library/Logs/neural-bridge/model-gateway.log`: method, path, status, duration, model, folded count; never request text, keys or headers |
| Restart | `launchctl kickstart -k gui/$(id -u)/com.andyherman.neural-bridge.model-gateway` |
| Bypass for one call | set `NB_COPILOT_API_BASE=http://localhost:4141` |
| Config | `NB_MODEL_GATEWAY_PORT`, `NB_MODEL_GATEWAY_UPSTREAM`, `NB_MODEL_GATEWAY_RESTART` (the launchd label restarted on a 403) |

Changes to `scripts/model_gateway.py` or its plist deploy through auto-reload like the daemon: `install.sh` re-bootstraps the service.

## Next

- **Per-agent models.** Every agent still defaults to `claude-opus-4.8`. The plan is Claude 5 per agent (in `agents.json`), after the gateway has run for a day, with 4.x as the fallback.
- **Moving off the Copilot seat** becomes an upstream change here, not a change across the fleet.
