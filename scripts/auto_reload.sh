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
# Safety:
# - Only runs `git pull --ff-only` on `main`. If you're on a feature branch,
#   the script logs and exits without touching anything.
# - Aborts cleanly on fetch/pull failures; daemon keeps running on the old code.
# - Append-only log at ~/Library/Logs/neural-bridge/auto-reload.log

set -euo pipefail

REPO="${HOME}/Development/neural-bridge"
LOG_DIR="${HOME}/Library/Logs/neural-bridge"
LOG="${LOG_DIR}/auto-reload.log"

mkdir -p "$LOG_DIR"

# Helper: write a timestamped line to the log.
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(ts) $*" >> "$LOG"; }

cd "$REPO"

# Only operate on main. If Andy is on a feature branch (mid-development),
# don't touch his working tree.
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
if [ "$BRANCH" != "main" ]; then
    log "skip: not on main (current: $BRANCH)"
    exit 0
fi

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
