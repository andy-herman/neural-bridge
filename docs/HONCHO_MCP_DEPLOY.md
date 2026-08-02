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

## Version pinning

The clone sits at upstream `85239a6` (2026-05-27). Upstream moves fast and the
API server runs alembic migrations — treat upgrades as a deliberate task
(fetch, read changelog/migrations, then `docker compose up -d --build`), not a
side effect of other work.
