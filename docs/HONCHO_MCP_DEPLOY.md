# Honcho Stack + MCP Worker — Deploy Runbook

**Date:** 2026-08-02
**Why this doc exists:** the MCP Worker's deploy config lives as *uncommitted*
local changes in a clone of upstream `plastic-labs/honcho` (upstream gitignores
nothing relevant, but the repo isn't ours to push to). If that clone is ever
re-cloned or cleaned, the config is gone. This runbook is the durable copy.

## Layout (consolidated 2026-08-02)

- **`~/Development/honcho`** — single clone of `plastic-labs/honcho`
  (was `~/Development/honcho-mcp-worker`; a second live clone at `~/Code/honcho`
  was retired to `~/Code/honcho-retired-2026-08-02` and can be deleted once
  the stack has been stable for a while).
- The directory name matters: docker compose derives the project name `honcho`
  from it, and the named volumes (`honcho_pgdata`, `honcho_redis-data`) that
  hold all peer memory are keyed by that project name. **Don't rename the
  directory** without `docker compose down` first and renaming back before `up`.

## Self-hosted stack

Runs from `~/Development/honcho` via untracked local files `docker-compose.yml`
and `.env` (copied from the old clone; upstream ships only `.example` variants).

```bash
cd ~/Development/honcho && docker compose up -d
```

Containers: `honcho-api-1` (:8001), `honcho-deriver-1`, `honcho-redis-1`,
`honcho-database-1` (pgvector). Data persists in named volumes — `docker
compose down` is safe, `down -v` destroys peer memory.

Exposure: cloudflared tunnel (launchd job
`com.andyherman.neural-bridge.honcho-tunnel`, config `~/.cloudflared/config.yml`)
maps `honcho.neural-bridge.dev` → `http://localhost:8001`. Tunnel is
path-independent — repo moves don't affect it.

## MCP Worker (`mcp.neural-bridge.dev`)

Lives in the `mcp/` subdirectory of the clone. Local (uncommitted) changes to
`mcp/wrangler.toml` — reproduce them if the clone is ever refreshed:

```toml
main = "src/index.ts"
compatibility_date = "2024-12-09"
compatibility_flags = ["nodejs_compat"]

# Opt out of workers.dev publishing. We deploy only to the custom route below.
workers_dev = false

# Point the Worker at the self-hosted Honcho exposed via Cloudflare Tunnel.
# Read by src/config.ts as the baseUrl for the Honcho SDK.
[vars]
HONCHO_API_URL = "https://honcho.neural-bridge.dev"

# Route under the neural-bridge.dev domain via Custom Domain (not Worker
# Route) so Cloudflare auto-creates the DNS record and manages the cert. The
# Worker Route variant requires DNS to already exist, which means a manual
# record add. custom_domain = true skips that step.
[[routes]]
pattern = "mcp.neural-bridge.dev"
custom_domain = true

[env.production]
name = "honcho-mcp"
workers_dev = false

[env.production.vars]
HONCHO_API_URL = "https://honcho.neural-bridge.dev"

[[env.production.routes]]
pattern = "mcp.neural-bridge.dev"
custom_domain = true
```

Deploy:

```bash
cd ~/Development/honcho/mcp && bunx wrangler deploy --env production
```

Health check: `https://mcp.neural-bridge.dev/` returns **401** (auth required)
when healthy; `https://honcho.neural-bridge.dev/` returns **404** (FastAPI has
no root route) when the tunnel + API are up.

## Deriver: flush mode is required at our traffic volume

Honcho's deriver uses forced batching: `representation` work units are only
claimed once they accumulate `REPRESENTATION_BATCH_MAX_TOKENS` worth of
messages. At personal-use volume (short messages spread across many
sessions/work units) batches never fill, so queue items pile up unprocessed
forever and peer cards stay empty — this is why cards were empty from May
through August 2026 (214 items stuck, zero errors logged; the deriver was
healthy and the Anthropic key fine the whole time).

Fix (set in `.env`, then `docker compose up -d deriver`):

```
DERIVER_FLUSH_ENABLED=true
```

Trade-off: more, smaller LLM calls to the deriver model (Haiku) instead of a
few big batched ones — negligible at this volume. If this line is ever lost
(re-clone, .env rebuild), the symptom is `SELECT count(*) FROM queue WHERE
processed=false` growing while `docker logs honcho-deriver-1` shows no errors.

Note the deriver/dialectic/summary models were switched from local qwen2.5:14b
to `claude-haiku-4-5` (Anthropic API) at some point after the May integration
doc — `docs/HONCHO_INTEGRATION.md`'s "when Andy adds an Anthropic key" section
is stale; the key is wired and only embeddings still run locally (Ollama bge-m3).

## Peer cards come from dreams, not the deriver

Two-stage pipeline: the **deriver** turns queue items into observation
`documents` (facts); the **dreamer** ("dream consolidation") later distills
documents into the peer card that `get_card()` returns, stored in
`peers.internal_metadata`. Dream triggers (all defaults): peer accumulates
≥50 new documents AND is idle ≥60 min, max one dream per 8h, `DREAM_ENABLED`
default true. So cards lag the conversation by an hour or more by design —
an empty card with thousands of documents just means no dream has run yet.

Debug queries:

```
docker exec honcho-database-1 psql -U postgres -c "SELECT count(*) FROM queue WHERE processed=false;"
docker exec honcho-database-1 psql -U postgres -c "SELECT observer, observed, count(*) FROM documents GROUP BY 1,2 ORDER BY 3 DESC;"
docker exec honcho-database-1 psql -U postgres -c "SELECT name FROM peers WHERE internal_metadata != '{}'::jsonb;"
```

## Version pinning

The clone sits at upstream `85239a6` (2026-05-27). Upstream moves fast and the
API server runs alembic migrations — treat upgrades as a deliberate task
(fetch, read changelog/migrations, then `docker compose up -d --build`), not a
side effect of other work.
