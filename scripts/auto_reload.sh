#!/bin/bash
# Auto-pull main and reload the Discord daemon if anything daemon-relevant
# changed. Runs every 2 minutes via launchd (com.andyherman.neural-bridge.auto-reload).
#
# Triggers a daemon reload when files in any of these paths changed since the
# last poll:
#   - scripts/discord_bot/*       (the daemon code itself)
#   - scripts/outbound_guard.py, scripts/fleet_heartbeat.py
#                                 (top-level modules the daemon imports)
#   - hooks/*                     (KNOWN_AGENTS + flush logic loaded at session boundaries)
#   - plugins/neural-bridge-core/agents/*  (charter changes)
#   - scripts/launchd/*           (plist edits — install.sh re-bootstraps)
#
# Skips reload (just logs the pull) for changes that don't affect the running
# daemon — README, docs/, knowledge/, blog content, tests, etc.
#
# Silent-staleness protection (#111):
#   When the working tree is on a feature branch, has uncommitted changes, or
#   cannot fast-forward, the watcher correctly refuses to pull main. But that
#   silent state lets the daemon run stale code indefinitely. After
#   NB_WATCHER_SILENT_TICKS_BEFORE_ALERT consecutive blocked ticks (default 15
#   ticks = 30 min), post a Discord ping. Only ping once per staleness window;
#   the counter resets once the checkout is back in step with origin/main.
#
#   Until 2026-09-25 only the feature-branch case counted, and the reset below
#   ended in `[ -f "$ALERTED_FLAG" ] && rm ...`, which returns 1 when the flag
#   is absent. Under `set -e` that killed every tick on main before the fetch,
#   with no log line and no stderr, from 2026-05-13 until it was found: four
#   months of merged PRs never reached the running daemon. The ERR trap below
#   exists so an early exit can never be silent again.
#
# Safety:
# - Only runs `git pull --ff-only` on `main`. If you're on a feature branch,
#   the script logs and exits without touching anything.
# - Aborts cleanly on fetch/pull failures; daemon keeps running on the old code.
# - Append-only log at ~/Library/Logs/neural-bridge/auto-reload.log

set -Eeuo pipefail

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

# Any command that trips `set -e` gets one log line naming where, so a dead
# watcher shows up in the log instead of as an unexplained launchd exit code.
trap 'rc=$?; log "FATAL: auto_reload.sh exited at line ${LINENO} (status ${rc})"' ERR

# Long-running services that import daemon code but that install.sh does not
# re-bootstrap. They are restarted in place after a daemon-relevant pull;
# without this, Luna on Telegram kept serving pre-2026-08-16 code for weeks.
RESTART_ALSO=(
    "com.andyherman.neural-bridge.luna-telegram"
    "com.andyherman.neural-bridge.loid-telegram"
    "com.andyherman.neural-bridge.council-telegram"
)

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
    # Called once the checkout is back in step with origin/main; the
    # silent-stale window is over. Remove counter + alerted flag so the next
    # divergence starts a fresh count. `rm -f` on a missing file succeeds, so
    # this can never return non-zero.
    rm -f "$SKIP_COUNTER" "$ALERTED_FLAG"
}

# Record one tick on which the daemon could not be brought up to date, and
# ping Discord once when the run of blocked ticks reaches THRESHOLD.
note_blocked() {
    local reason="$1"
    local count
    count=$(read_count)
    count=$((count + 1))
    write_count "$count"

    if [ "$count" -eq "$THRESHOLD" ]; then
        # Crossed the threshold this tick. Ping Discord once and mark.
        local minutes=$((count * 2))
        log "alert: blocked $count ticks (~${minutes} min): $reason — pinging Discord"
        local msg="⚠️ neural-bridge auto-reload has been blocked for ${count} ticks (~${minutes} min): ${reason}. The daemon is running stale code until that is resolved in ${REPO}."
        if post_discord_alert "$msg"; then
            touch "$ALERTED_FLAG"
        else
            log "alert: discord post failed (webhook missing or HTTP error) — daemon still silent"
        fi
    elif [ -f "$ALERTED_FLAG" ]; then
        # Already alerted; quiet log to avoid spam.
        log "skip: $reason (count=$count, already alerted)"
    else
        log "skip: $reason (count=$count/$THRESHOLD)"
    fi
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
# don't touch his working tree; count the tick as blocked instead.
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
if [ "$BRANCH" != "main" ]; then
    note_blocked "working tree is on branch '${BRANCH}', not main"
    exit 0
fi

# Snapshot HEAD before fetch.
HEAD_BEFORE=$(git rev-parse HEAD)

# Fetch quietly. If the network is down, abort cleanly. A network blip is not
# a staleness signal, so it does not touch the blocked counter.
if ! git fetch --quiet origin main 2>>"$LOG"; then
    log "fetch failed (network or auth) — daemon untouched"
    exit 0
fi

HEAD_REMOTE=$(git rev-parse origin/main)
if [ "$HEAD_BEFORE" = "$HEAD_REMOTE" ]; then
    # Nothing new on origin — the daemon's code is current. Quiet exit.
    reset_skip_state
    exit 0
fi

# Bail if there are local uncommitted changes (don't blow them away with
# --ff-only). This is the state that hid the Sep 2026 gap, so it counts.
if ! git diff --quiet || ! git diff --cached --quiet; then
    note_blocked "working tree has uncommitted changes and main is behind origin"
    exit 0
fi

# Pull.
if ! git pull --ff-only --quiet origin main 2>>"$LOG"; then
    note_blocked "git pull --ff-only failed (see log for git's reason)"
    exit 0
fi
reset_skip_state

# What actually changed?
CHANGED=$(git diff --name-only "$HEAD_BEFORE..HEAD")
log "pulled $(echo "$CHANGED" | wc -l | tr -d ' ') files: $HEAD_BEFORE → $(git rev-parse HEAD | cut -c1-7)"

# Decide whether to reload. Match files that the running daemons actually load.
DAEMON_RELEVANT=0
while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
        scripts/discord_bot/*|scripts/telegram_bot/*|scripts/luna/*|scripts/env_file.py|scripts/outbound_guard.py|scripts/fleet_heartbeat.py|hooks/*|plugins/neural-bridge-core/agents/*|scripts/launchd/*)
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

# Never let install.sh re-bootstrap this job: booting out the running job
# kills this script before it finishes. A changed auto-reload plist therefore
# takes effect on the next manual install.sh or reboot, and the log says so.
SELF_LABEL="com.andyherman.neural-bridge.auto-reload"
if NB_INSTALL_SKIP="$SELF_LABEL" ./scripts/launchd/install.sh >> "$LOG" 2>&1; then
    log "reload done"
else
    log "WARN: install.sh exited non-zero — check log for traceback"
fi
if grep -qx "scripts/launchd/${SELF_LABEL}.plist" <<< "$CHANGED"; then
    log "note: ${SELF_LABEL}.plist changed; run scripts/launchd/install.sh by hand to apply it"
fi

# Restart the bridges install.sh does not own. Only the ones actually loaded;
# a service that is not installed on this machine is skipped, not an error.
for label in "${RESTART_ALSO[@]}"; do
    if launchctl print "gui/$(id -u)/${label}" >/dev/null 2>&1; then
        if launchctl kickstart -k "gui/$(id -u)/${label}" >> "$LOG" 2>&1; then
            log "restarted ${label}"
        else
            log "WARN: kickstart failed for ${label}"
        fi
    fi
done
