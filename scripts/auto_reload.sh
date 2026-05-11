#!/bin/bash
# Auto-pull main and reload the Discord daemon if anything daemon-relevant
# changed. Runs every 2 minutes via launchd (com.andyherman.neural-bridge.auto-reload).
#
# Triggers a daemon reload when files in any of these paths changed since the
# last poll:
#   - scripts/discord_bot/*       (the daemon code itself)
#   - hooks/*                     (KNOWN_AGENTS + flush logic loaded at session boundaries)
#   - plugins/neural-bridge-core/agents/*  (charter changes)
#   - scripts/launchd/*           (plist edits — install.sh re-bootstraps)
#
# Skips reload (just logs the pull) for changes that don't affect the running
# daemon — README, docs/, knowledge/, blog content, tests, etc.
#
# Silent-staleness protection (#111):
#   When the working tree is on a feature branch, the watcher correctly
#   refuses to pull main. But that silent state lets the daemon run stale
#   code indefinitely. After NB_WATCHER_SILENT_TICKS_BEFORE_ALERT consecutive
#   skips (default 15 ticks = 30 min), post a Discord ping. Only ping once
#   per staleness window — counter resets when the branch returns to main.
#
# Safety:
# - Only runs `git pull --ff-only` on `main`. If you're on a feature branch,
#   the script logs and exits without touching anything.
# - Aborts cleanly on fetch/pull failures; daemon keeps running on the old code.
# - Append-only log at ~/Library/Logs/neural-bridge/auto-reload.log

set -euo pipefail

REPO="${HOME}/Development/neural-bridge"
LOG_DIR="${HOME}/Library/Logs/neural-bridge"
LOG="${LOG_DIR}/auto-reload.log"
SKIP_COUNTER="${LOG_DIR}/auto-reload.skip-count"
ALERTED_FLAG="${LOG_DIR}/auto-reload.alerted"
THRESHOLD="${NB_WATCHER_SILENT_TICKS_BEFORE_ALERT:-15}"
KEYCHAIN_WEBHOOK_SERVICE="neural-bridge-discord-webhook"

mkdir -p "$LOG_DIR"

# Helper: write a timestamped line to the log.
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(ts) $*" >> "$LOG"; }

# Helper: read the skip counter (default 0 if file missing).
read_count() {
    if [ -f "$SKIP_COUNTER" ]; then
        cat "$SKIP_COUNTER" 2>/dev/null || echo 0
    else
        echo 0
    fi
}

write_count() {
    echo "$1" > "$SKIP_COUNTER"
}

reset_skip_state() {
    # Called when we make it past the branch check; the silent-stale window
    # is over. Remove counter + alerted flag so the next divergence starts
    # a fresh count.
    [ -f "$SKIP_COUNTER" ] && rm -f "$SKIP_COUNTER"
    [ -f "$ALERTED_FLAG" ] && rm -f "$ALERTED_FLAG"
}

# Helper: POST to Discord webhook resolved from keychain. Same source as
# hooks/discord_post.py. Silent-fails if no webhook is configured — the
# main script keeps running.
post_discord_alert() {
    local message="$1"
    local webhook_url
    webhook_url=$(security find-generic-password \
        -s "$KEYCHAIN_WEBHOOK_SERVICE" -a "$USER" -w 2>/dev/null) || return 1
    [ -z "$webhook_url" ] && return 1
    # Escape double-quotes + backslashes for JSON. The message is built by
    # this script, so it's tightly controlled, but quote-escape defensively.
    local escaped
    escaped=$(printf '%s' "$message" | python3 -c 'import json, sys; sys.stdout.write(json.dumps(sys.stdin.read()))')
    curl -fsS -X POST \
        -H "Content-Type: application/json" \
        -d "{\"content\":${escaped}}" \
        "$webhook_url" >/dev/null 2>&1
}

cd "$REPO"

# Only operate on main. If Andy is on a feature branch (mid-development),
# don't touch his working tree. Track silent-staleness via a counter:
# escalate to a Discord ping after THRESHOLD ticks (~30 min default).
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
if [ "$BRANCH" != "main" ]; then
    count=$(read_count)
    count=$((count + 1))
    write_count "$count"

    if [ "$count" -eq "$THRESHOLD" ]; then
        # Crossed the threshold this tick. Ping Discord once and mark.
        minutes=$((count * 2))
        log "alert: skipped $count ticks (~${minutes} min) on '$BRANCH' — pinging Discord"
        msg="⚠️ neural-bridge daemon auto-reload has skipped ${count} ticks (~${minutes} min) because the working tree is on branch \`${BRANCH}\`. The daemon is running stale code until you \`git checkout main\` in ${REPO}."
        if post_discord_alert "$msg"; then
            touch "$ALERTED_FLAG"
        else
            log "alert: discord post failed (webhook missing or HTTP error) — daemon still silent"
        fi
    elif [ -f "$ALERTED_FLAG" ]; then
        # Already alerted; quiet log to avoid spam.
        log "skip: still on '$BRANCH' (count=$count, already alerted)"
    else
        log "skip: not on main (current: $BRANCH, count=$count/$THRESHOLD)"
    fi
    exit 0
fi

# Got past the branch check — staleness window (if any) is over.
reset_skip_state

# Snapshot HEAD before fetch.
HEAD_BEFORE=$(git rev-parse HEAD)

# Fetch quietly. If the network is down, abort cleanly.
if ! git fetch --quiet origin main 2>>"$LOG"; then
    log "fetch failed (network or auth) — daemon untouched"
    exit 0
fi

HEAD_REMOTE=$(git rev-parse origin/main)
if [ "$HEAD_BEFORE" = "$HEAD_REMOTE" ]; then
    # Nothing new on origin — quiet exit.
    exit 0
fi

# Bail if there are local uncommitted changes (don't blow them away with --ff-only).
if ! git diff --quiet || ! git diff --cached --quiet; then
    log "skip: working tree dirty — daemon untouched, manual reload after committing"
    exit 0
fi

# Pull.
if ! git pull --ff-only --quiet origin main 2>>"$LOG"; then
    log "pull failed — daemon untouched"
    exit 0
fi

# What actually changed?
CHANGED=$(git diff --name-only "$HEAD_BEFORE..HEAD")
log "pulled $(echo "$CHANGED" | wc -l | tr -d ' ') files: $HEAD_BEFORE → $(git rev-parse HEAD | cut -c1-7)"

# Decide whether to reload. Match files that the running daemon actually loads.
DAEMON_RELEVANT=0
while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
        scripts/discord_bot/*|hooks/*|plugins/neural-bridge-core/agents/*|scripts/launchd/*)
            DAEMON_RELEVANT=1
            break
            ;;
    esac
done <<< "$CHANGED"

if [ "$DAEMON_RELEVANT" -eq 0 ]; then
    log "no daemon-relevant changes (skip reload)"
    exit 0
fi

log "reloading daemon (changed files include daemon code/config)"
echo "$CHANGED" | sed 's/^/  - /' >> "$LOG"

if ./scripts/launchd/install.sh >> "$LOG" 2>&1; then
    log "reload done"
else
    log "WARN: install.sh exited non-zero — check log for traceback"
fi
