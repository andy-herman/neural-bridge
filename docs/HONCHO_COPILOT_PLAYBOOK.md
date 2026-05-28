# Honcho → GitHub Copilot via Cloudflare Tunnel + MCP Worker

How to expose the locally-running Honcho instance to GitHub Copilot's Coding Agent so it has the same Andy peer-model that NB and Hermes already use.

**Companion to:** `HONCHO_INTEGRATION.md` (which covers the daemon-side wiring; local NB agents already use Honcho without any of this).

## Architecture

```
GitHub Copilot Coding Agent
  └─ MCP request ────────────────────────────►  honcho.neural-bridge.dev (Cloudflare edge)
                                                         │
                                                         │  (Cloudflare Access service-token check)
                                                         ▼
                                              Cloudflare Worker (Honcho MCP)
                                                         │
                                                         │  (Worker reads HONCHO_API_URL env)
                                                         ▼
                                              Cloudflare Tunnel ──► localhost:8001 on Mac mini
                                                                          │
                                                                          ▼
                                                               Honcho API (Docker, restart: unless-stopped)
```

Two distinct components on the cloud side:
1. **Cloudflare Tunnel** — outbound-only connection from your Mac to Cloudflare's edge. No inbound ports opened. Routes `honcho.neural-bridge.dev` → `http://localhost:8001`.
2. **Cloudflare Worker (Honcho MCP)** — translates MCP calls from Copilot into Honcho API calls against the tunneled endpoint.

## Security baseline (READ FIRST)

Today, your self-hosted Honcho runs **without API-key auth** (`api_key="self-hosted"` literal in `honcho_client.py`). Exposing the tunnel publicly without a layer in front means anyone who guesses the URL can read and write your peer memory.

**Required before opening the tunnel:** put Cloudflare Access in front. This adds a service-token check at the edge so only Copilot's Worker (with the token in its env) can reach Honcho. Setup is one-time, ~5 min, in the Cloudflare Zero Trust dashboard.

Alternative: turn on Honcho's own API-key auth and pass the key as both the MCP Worker's `HONCHO_API_KEY` and the `HONCHO_API_KEY` env in the daemon's launchd plist. Either approach works; Cloudflare Access is the cleaner one because it stops unauthorized traffic before it even reaches your machine.

## Step-by-step

### 0. Pre-flight (already done)

- `cloudflared` installed (`brew install cloudflared`, version 2026.5.2+)
- Honcho running on `localhost:8001` (verified earlier)
- Domain `neural-bridge.dev` is on Cloudflare (confirmed since the blog already runs there)

### 1. Authenticate cloudflared (one-time, browser flow)

```
cloudflared tunnel login
```

Opens a browser. Sign in to Cloudflare and authorize the `neural-bridge.dev` zone. Drops a cert at `~/.cloudflared/cert.pem`. **This is the only step requiring your interaction; everything below is scripted.**

### 2. Create the tunnel, route DNS, install the launchd job

Run the prepared script:

```
./scripts/cloudflared/setup-honcho-tunnel.sh
```

The script does:
1. Creates a named tunnel `honcho-bridge` (stores credentials at `~/.cloudflared/<UUID>.json`).
2. Writes `~/.cloudflared/config.yml` routing `honcho.neural-bridge.dev` → `http://localhost:8001`.
3. Adds a DNS CNAME (`honcho.neural-bridge.dev` → `<UUID>.cfargotunnel.com`).
4. Installs the launchd user agent so the tunnel auto-starts at login.

Verify:

```
curl -s https://honcho.neural-bridge.dev/openapi.json | head -5
```

Should return Honcho's OpenAPI JSON. If it 522s or hangs, the tunnel didn't come up — check `~/Library/Logs/neural-bridge/honcho-tunnel.stderr.log`.

### 3. Put Cloudflare Access in front of the tunnel

In the Cloudflare Zero Trust dashboard (`one.dash.cloudflare.com`):
1. Access → Applications → Add Application → Self-hosted
2. Application domain: `honcho.neural-bridge.dev`
3. Add an Access Policy → Action: Service Auth → Include: Service Token (create one named `copilot-honcho-mcp`)
4. Save. Copy the service-token Client ID + Client Secret.

After this, all requests to `honcho.neural-bridge.dev` without the service-token headers will be rejected with HTTP 403 at the edge. Verify:

```
curl -s -o /dev/null -w "%{http_code}\n" https://honcho.neural-bridge.dev/openapi.json
# Expect: 403 (no service token = blocked)

curl -s -o /dev/null -w "%{http_code}\n" \
  -H "CF-Access-Client-Id: <CLIENT_ID>" \
  -H "CF-Access-Client-Secret: <CLIENT_SECRET>" \
  https://honcho.neural-bridge.dev/openapi.json
# Expect: 200
```

### 4. Deploy the Honcho MCP Cloudflare Worker

```
cd ~/Development
git clone https://github.com/plastic-labs/honcho honcho-mcp-worker
cd honcho-mcp-worker/mcp
npx wrangler login  # browser auth, second time
```

Configure the Worker by adding to `wrangler.toml` (or set via dashboard):

```toml
[vars]
HONCHO_API_URL = "https://honcho.neural-bridge.dev"
```

Set the two service-token secrets (these get sent as Access headers to reach the tunnel):

```
npx wrangler secret put CF_ACCESS_CLIENT_ID
# paste the Client ID from step 3

npx wrangler secret put CF_ACCESS_CLIENT_SECRET
# paste the Client Secret from step 3
```

The upstream Worker may not have CF-Access header forwarding built in. If not, fork it or add a 5-line patch to `src/index.ts` that injects:

```typescript
headers: {
  ...originalHeaders,
  "CF-Access-Client-Id": env.CF_ACCESS_CLIENT_ID,
  "CF-Access-Client-Secret": env.CF_ACCESS_CLIENT_SECRET,
}
```

Deploy:

```
npx wrangler deploy
```

The deploy output prints the Worker URL (something like `https://honcho-mcp.<your-subdomain>.workers.dev`). Save it for step 5.

### 5. Register the MCP server in the NB repo's Copilot settings

GitHub → `andy-herman/neural-bridge` → Settings → Copilot → MCP servers.

Paste:

```json
{
  "honcho": {
    "url": "https://honcho-mcp.<your-subdomain>.workers.dev/mcp",
    "type": "http"
  }
}
```

If Copilot is configured to need additional auth between Copilot's runner and the Worker, add it here. The Worker itself is already authenticated to Honcho via the service token from step 4.

### 6. Verify end-to-end

Open a test issue in the NB repo and assign it to Copilot. In its working notes you should see tool calls like `mcp__honcho__get_peer_card` returning Andy's peer card.

## What to keep an eye on

- **Tunnel uptime.** `launchctl print gui/$(id -u)/com.andyherman.neural-bridge.honcho-tunnel` should show `state = running`. The cloudflared client auto-reconnects if Cloudflare's edge restarts, but if the daemon dies, the tunnel dies.
- **Honcho schema drift.** The Worker is pinned to a specific honcho-ai SDK version. If you upgrade your self-hosted Honcho beyond that, the Worker may break on new endpoints. Pin both sides or update together.
- **Service-token rotation.** Cloudflare service tokens have a default 1-year expiry. Calendar a rotation reminder.

## Rollback

To unwire without breaking the local NB daemon (which doesn't depend on any of this):

```
# Remove the tunnel
launchctl bootout gui/$(id -u)/com.andyherman.neural-bridge.honcho-tunnel
rm ~/Library/LaunchAgents/com.andyherman.neural-bridge.honcho-tunnel.plist
cloudflared tunnel delete honcho-bridge

# Remove the DNS record + Access app in the Cloudflare dashboard

# Remove the Worker
cd ~/Development/honcho-mcp-worker/mcp && npx wrangler delete

# Remove the MCP server from the NB repo Copilot settings
```

The local NB daemon's `honcho_client.py` stays wired to `localhost:8001`. Nothing on the daemon side is affected.
