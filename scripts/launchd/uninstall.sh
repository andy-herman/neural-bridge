#!/bin/bash
# Uninstall all Neural Bridge launchd user agents.

set -euo pipefail

AGENTS=(
    "com.andyherman.neural-bridge.discord-bot"
    "com.andyherman.neural-bridge.publish-prep"
    "com.andyherman.neural-bridge.compile-nightly"
    "com.andyherman.neural-bridge.auto-reload"
    "com.andyherman.neural-bridge.summarize-weekly"
)

uninstall_agent() {
    local label="$1"
    local plist="${label}.plist"
    local target_path="${HOME}/Library/LaunchAgents/${plist}"

    echo "Uninstalling ${label}..."

    if launchctl print "gui/$(id -u)/${label}" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)/${label}"
        echo "  agent unloaded"
    else
        echo "  agent was not loaded"
    fi

    if [[ -f "${target_path}" ]]; then
        rm -f "${target_path}"
        echo "  removed ${target_path}"
    else
        echo "  no plist at ${target_path}"
    fi
    echo ""
}

for label in "${AGENTS[@]}"; do
    uninstall_agent "${label}"
done

echo "Done. Logs at ~/Library/Logs/neural-bridge/ are kept (delete manually if you want)."
