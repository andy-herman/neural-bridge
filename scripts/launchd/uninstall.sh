#!/bin/bash
# Uninstall the Neural Bridge Discord bot launchd user agent.

set -euo pipefail

PLIST_NAME="com.andyherman.neural-bridge.discord-bot.plist"
PLIST_LABEL="com.andyherman.neural-bridge.discord-bot"
TARGET_PATH="${HOME}/Library/LaunchAgents/${PLIST_NAME}"

echo "Uninstalling ${PLIST_LABEL}..."

if launchctl print "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/${PLIST_LABEL}"
    echo "  agent unloaded"
else
    echo "  agent was not loaded"
fi

if [[ -f "${TARGET_PATH}" ]]; then
    rm -f "${TARGET_PATH}"
    echo "  removed ${TARGET_PATH}"
else
    echo "  no plist at ${TARGET_PATH}"
fi

echo ""
echo "Done. Logs at ~/Library/Logs/neural-bridge/ are kept (delete manually if you want)."
