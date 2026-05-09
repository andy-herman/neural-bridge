#!/bin/bash
# Install the Neural Bridge Discord bot as a launchd user agent.
#
# Idempotent: safe to run multiple times. If the agent is already loaded,
# it bootstraps cleanly (uninstalls then reinstalls).

set -euo pipefail

PLIST_NAME="com.andyherman.neural-bridge.discord-bot.plist"
PLIST_LABEL="com.andyherman.neural-bridge.discord-bot"
SOURCE_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${PLIST_NAME}"
TARGET_PATH="${HOME}/Library/LaunchAgents/${PLIST_NAME}"
LOG_DIR="${HOME}/Library/Logs/neural-bridge"

echo "Installing ${PLIST_LABEL}..."

if [[ ! -f "${SOURCE_PATH}" ]]; then
    echo "ERROR: source plist not found at ${SOURCE_PATH}" >&2
    exit 1
fi

# Pre-flight: venv exists?
VENV_PYTHON="${HOME}/Development/neural-bridge/.venv/bin/python"
if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "ERROR: ${VENV_PYTHON} not found." >&2
    echo "Run this first:" >&2
    echo "  cd ~/Development/neural-bridge" >&2
    echo "  python3 -m venv .venv" >&2
    echo "  source .venv/bin/activate" >&2
    echo "  pip3 install -r scripts/discord_bot/requirements.txt" >&2
    exit 1
fi

# Ensure log dir.
mkdir -p "${LOG_DIR}"

# Copy the plist into LaunchAgents.
mkdir -p "${HOME}/Library/LaunchAgents"
cp "${SOURCE_PATH}" "${TARGET_PATH}"
echo "  copied plist to ${TARGET_PATH}"

# Bootstrap (or re-bootstrap if already loaded).
if launchctl print "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1; then
    echo "  agent already loaded; unloading first..."
    launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" || true
fi

launchctl bootstrap "gui/$(id -u)" "${TARGET_PATH}"
echo "  bootstrapped"

# Verify.
sleep 1
if launchctl print "gui/$(id -u)/${PLIST_LABEL}" >/dev/null 2>&1; then
    echo "  status: loaded"
else
    echo "  status: NOT LOADED — check ${LOG_DIR}/discord-bot.stderr.log" >&2
    exit 1
fi

echo ""
echo "Done. Tail logs with:"
echo "  tail -f ${LOG_DIR}/discord-bot.stderr.log"
echo ""
echo "Uninstall with:"
echo "  ./scripts/launchd/uninstall.sh"
