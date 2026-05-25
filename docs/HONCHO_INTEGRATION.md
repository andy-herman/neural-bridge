# Honcho Integration — Shared Peer Memory Across NB Agents

**Status:** Wired but unmerged. Review before committing.
**Date:** 2026-05-24
**Scope:** Shared `andyherman` peer card across all NB agents + Hermes-side Yor.

## What this does

All NB agents (Luna, research, content, social, senior-pm, etc.) now read from and write to the same Honcho instance that Yor uses on the Hermes side. The single workspace `hermes` holds:

- One **user peer** `andyherman` — the persistent model of Andy that compounds across every conversation with every agent
- One **AI peer per NB agent** (`luna`, `research`, `content`, ...) plus `yor` from Hermes — each contributing observations
- Honcho's **directional mode** keeps each AI peer's view of Andy distinct, but the underlying facts accumulate in a shared workspace

Result: when Andy tells Luna in DM "I think in arcs not lists" and Yor on Telegram learns "Andy avoids killing started ideas," both facts end up in the same Honcho workspace. Any agent in either runtime can ground its replies in the union.

## What changed

Three files:

| File | Change | Lines |
|---|---|---|
| `scripts/discord_bot/honcho_client.py` | **NEW** — thin wrapper around honcho-ai SDK. Lazy init, graceful degradation, env-configured. | ~160 |
| `scripts/discord_bot/mention.py` | Import + 1 prepend in `build_mention_prompt()` to inject peer card context above the lessons digest, for **every agent**. | ~10 |
| `scripts/discord_bot/handlers.py` | Import + `honcho_client.submit_turn()` call after a successful Discord post, for **every agent**. | ~15 |

No existing logic was removed or rewritten. The integration is a layer added on top of the existing prompt/memory pipeline.

## Environment

The bot picks up Honcho with **zero new env vars** if Andy is running the default localhost setup (which he is). Optional overrides:

| Var | Default | Purpose |
|---|---|---|
| `HONCHO_ENABLED` | `true` | Set `false` to disable the integration without removing code |
| `HONCHO_BASE_URL` | `http://localhost:8001` | Honcho API location |
| `HONCHO_WORKSPACE` | `hermes` | Workspace ID (matches Yor's) |
| `HONCHO_USER_PEER` | `andyherman` | User peer ID (matches Yor's) |
| `HONCHO_API_KEY` | empty | For future self-hosted-with-auth or cloud deploys |
| `HONCHO_TIMEOUT` | `180` | Seconds; bumped from default 60 because qwen2.5:14b is slow on M4. Drop after switching deriver to Anthropic. |

## Failure modes (all graceful)

- Honcho server unreachable → empty context returned, submit silently skipped, bot keeps working
- `honcho-ai` SDK not installed in venv → one-shot warning logged, all calls no-op
- Honcho API returns an error → debug-level log, no exception propagates
- Peer card empty (e.g. deriver hasn't run yet) → empty context returned, prompt unchanged

## Dependency

Added `honcho-ai` to the NB venv via direct `pip install`. NB has no `pyproject.toml` / `requirements.txt`, so deps live in the venv only. If the venv is rebuilt, re-run:

```
.venv/bin/pip install honcho-ai
```

## Verification when Anthropic API key is wired

Right now Honcho's deriver runs on `qwen2.5:14b` (local Ollama, slow, frequent timeouts). When Andy adds an Anthropic key and we swap Honcho to Haiku 4.5:

1. Restart the Honcho stack (`docker compose restart` in `~/Code/honcho`).
2. Send Luna a substantive Discord message about Andy's preferences. Wait ~30s.
3. Check the deriver activity: `docker logs -f honcho-deriver-1`. Should see Anthropic calls completing in 1-3s instead of timing out.
4. Wait ~10-30s for the deriver to process.
5. Verify the peer card populated:
   ```
   .venv/bin/python -c "from honcho import Honcho; c = Honcho(workspace_id='hermes', base_url='http://localhost:8001', api_key='none'); print(c.peer('luna').get_card('andyherman'))"
   ```
6. Send Luna another message — her reply should now reflect facts in the card (the auto-injected context block at the top of the prompt).
7. Send Yor a message on Telegram or Discord — she should ALSO benefit from observations Luna captured (same workspace, same user peer).

## Known caveats

1. **Directional observation mode siloes per-agent views.** Luna's facts about Andy are stored under `luna`'s perspective; Yor's under `yor`'s. The integration falls back to the global andyherman card if a per-agent view is empty, but full cross-agent sharing would require switching the observation mode to `unified` in `~/.hermes/honcho.json`. Worth revisiting after a week of data — if directional silos feel limiting, flip to unified and re-derive.
2. **Honcho's `get_card()` returns None until the deriver has produced facts.** Cold start = no context for ~the first few conversations.
3. **No prompt-injection sanitization on Honcho-supplied context.** The peer card is LLM-generated from Andy's own messages, so the threat surface is low — but worth thinking about if you ever expose Honcho writes to other humans.
4. **Submit happens after Discord post, fire-and-forget.** If the Honcho API stalls, the bot is unaffected; the turn just doesn't get captured. Future improvement: optional retry queue.

## Rollback

`git diff` and revert. The new file `honcho_client.py` can be deleted; the two existing-file edits revert cleanly. Bot continues working with no Honcho layer.

## Files for review

```
scripts/discord_bot/honcho_client.py   (new)
scripts/discord_bot/mention.py         (+~10 lines: import + 1 prepend block)
scripts/discord_bot/handlers.py        (+~15 lines: import + 1 submit call)
docs/HONCHO_INTEGRATION.md             (this file)
```

`git diff --stat` will show the exact scope. No production data touched; this is pure plumbing.
