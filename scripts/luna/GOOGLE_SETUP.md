# Giving Luna calendar and inbox access

One-time setup. Roughly 10 minutes, and every step needs your Google account,
so none of it can be automated by an agent.

## Why this exists

Luna's charter says she owns your calendar and inbox. Her tool allowlist
hand-enumerates eighteen `mcp__claude_ai_*` names for Calendar, Gmail and Drive.
**None of them work.** `--allowedTools` only auto-approves tools that already
exist, and claude.ai connectors are not loaded into a headless `claude -p`
subprocess. Tested 2026-08-15: asked for today's events, she replies
`NO_CALENDAR_ACCESS`.

This replaces them with real CLIs plus Bash, which is the pattern that already
works for Loid and `synapse-journal`.

## What Luna gets

| Command | What it does |
|---|---|
| `python -m scripts.luna.calendar today` | today's events, flags meetings with no agenda |
| `python -m scripts.luna.calendar week --days 7` | the week ahead |
| `python -m scripts.luna.calendar next --count 3` | the next few things |
| `python -m scripts.luna.calendar conflicts` | overlapping meetings |
| `python -m scripts.luna.inbox unread --count 10` | unread mail |
| `python -m scripts.luna.inbox search "from:x"` | Gmail search syntax |
| `python -m scripts.luna.inbox thread <id>` | one conversation |
| `python -m scripts.luna.inbox waiting --days 5` | threads you sent and nobody answered |

`waiting` is the one that earns its keep. It is the thing an assistant tracks
and a person forgets.

## What Luna does NOT get

**No send. No delete. No event creation.** Not "not yet configured", not
present in the code.

- Her charter is explicit that Gmail is draft-only and she never sends.
- Her old allowlist granted `delete_event` with nothing in her charter
  authorizing deletion. The August audit flagged it; an assistant that can
  silently drop a meeting is a liability.
- Drafting is absent too, for a specific reason: Google has no
  draft-without-send scope. `gmail.compose` grants both in one consent. So the
  default request is `gmail.readonly`, and there is no write path at all.

Tests assert all of this at the argparse level, so adding a send or delete
command breaks the suite rather than slipping through review.

## Step 1. Create a Google Cloud project and OAuth client

1. Go to <https://console.cloud.google.com/projectcreate>, make a project
   (name it something like `luna-assistant`).
2. Enable both APIs:
   - <https://console.cloud.google.com/apis/library/calendar-json.googleapis.com>
   - <https://console.cloud.google.com/apis/library/gmail.googleapis.com>
3. Configure the OAuth consent screen: **External**, publishing status
   **In production**.

   Two traps here, both hit for real on 2026-08-15:

   **Do not pick Internal**, even though the console offers it. It is offered
   whenever the project sits under an organization, and `luna-assistant` does
   (`andy-herman17-org`). But the org is attached to the *project*, not to a
   consumer `@gmail.com` *identity*. The selection is accepted and then consent
   fails at the last step with `Error 403: org_internal`.

   **Do not leave it in Testing.** Testing is the obvious choice for a one-user
   app and it works on the day you set it up. Then refresh tokens for sensitive
   scopes expire after 7 days and Luna goes dark every week. In production has
   no such expiry. The cost is an "unverified app" warning at consent, which is
   correct and expected for an app you published yourself and never submitted
   for review: click **Advanced**, then **Go to Luna Assistant (unsafe)**.

   The 100-user lifetime cap shown on the Audience page is irrelevant for a
   single-user app.
4. Credentials → Create credentials → **OAuth client ID** → **Desktop app**.
5. Download the JSON.

## Step 2. Put the client secret where the tools look

```bash
mkdir -p ~/.config/neural-bridge
mv ~/Downloads/client_secret_*.json ~/.config/neural-bridge/google_client_secret.json
chmod 600 ~/.config/neural-bridge/google_client_secret.json
```

## Step 3. Authorize

```bash
cd ~/Development/neural-bridge
.venv/bin/python -m scripts.luna.google_auth setup
```

A browser opens, you approve, and a token lands at
`~/.config/neural-bridge/google_token.json` with mode 600. The command prints
the refresh token only in redacted form.

At the consent screen: pick the account, then click **Advanced** and **Go to
Luna Assistant (unsafe)** past the unverified-app warning, then **Allow**.

The command waits 15 minutes for the redirect and then gives up cleanly,
releasing the port. Nothing is lost on a timeout; just run it again when you are
actually at the keyboard. Override with `--wait <seconds>` if you want longer.

## Step 4. Verify

```bash
.venv/bin/python -m scripts.luna.google_auth status     # expect: configured
.venv/bin/python -m scripts.luna.calendar today
.venv/bin/python -m scripts.luna.inbox unread --count 3
```

## Step 5. Let Luna actually call them

Until this step she still cannot reach any of it. Two changes:

1. Grant her `Bash` in `MENTION_ALLOWED_TOOLS` (`scripts/discord_bot/mention.py`).
   Note the tradeoff honestly: that is unrestricted shell, not shell scoped to
   these two scripts. Loid already holds Bash for the same reason and there is a
   documented test exemption for it. The narrower fix is a `PreToolUse` hook
   that allowlists the two commands, which is the deferred item in
   `docs/LOOP_ENGINEER.md`.
2. Tell her the commands exist, in her charter, with one line each.

Then drop the eighteen dead `mcp__claude_ai_*` entries from her allowlist. They
have never resolved and they make the charter look truthful when it is not.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `not configured: no client secret` | step 2 not done |
| `not authorized: ... no token` | step 3 not done |
| `token file has no refresh_token` | revoke at <https://myaccount.google.com/permissions>, redo step 3 |
| `API 403: insufficient authentication scopes` | scopes changed since authorizing; redo step 3 |
| `token endpoint returned 400: invalid_grant` | refresh token revoked or expired; redo step 3 |
| `Error 403: org_internal` at consent | audience is set to Internal; switch to External on the Audience page |
| `cannot listen on http://localhost:8765` | a previous setup run is still holding the port: `pkill -f scripts.luna.google_auth` |
| Luna worked for a week then stopped | publishing status slipped back to Testing; set it to In production |

## Rotating or revoking

Revoke at <https://myaccount.google.com/permissions>, then
`rm ~/.config/neural-bridge/google_token.json`. The tools fail closed with
`CALENDAR_UNAVAILABLE` / `INBOX_UNAVAILABLE` rather than degrading silently,
which is deliberate: a memory layer that failed quietly for ten weeks is what
prompted the instrumentation work in the first place.
