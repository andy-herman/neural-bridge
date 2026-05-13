#!/bin/bash
# Install Neural Bridge launchd user agents.
#
# Currently installs:
#   - com.andyherman.neural-bridge.discord-bot        (always-on Discord daemon)
#   - com.andyherman.neural-bridge.publish-prep       (Sunday 18:00 PT publish prep)
#   - com.andyherman.neural-bridge.compile-nightly    (03:00 daily concept compile)
#   - com.andyherman.neural-bridge.auto-reload        (every 2 min: pull main + reload daemon if daemon-relevant files changed)
#   - com.andyherman.neural-bridge.summarize-weekly   (Monday 04:00: compress prior week of conversation logs into per-agent lessons-learned digests)
#   - com.andyherman.neural-bridge.echo-synthesis     (Sunday 05:00: synthesize new Discord messages into Echo's structured profile files)
#
# Idempotent: safe to run multiple times. Re-bootstraps any agent that's
# already loaded.

set -euo pipefail

AGENTS=(
    "com.andyherman.neural-bridge.discord-bot"
    "com.andyherman.neural-bridge.publish-prep"
    "com.andyherman.neural-bridge.compile-nightly"
    "com.andyherman.neural-bridge.auto-reload"
    "com.andyherman.neural-bridge.summarize-weekly"
    "com.andyherman.neural-bridge.echo-synthesis"
)

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
LOG_DIR="${HOME}/Library/Logs/neural-bridge"
VENV_PYTHON="${HOME}/Development/neural-bridge/.venv/bin/python"

# Pre-flight: venv exists?
if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "ERROR: ${VENV_PYTHON} not found." >&2
    echo "Run this first:" >&2
    echo "  cd ~/Development/neural-bridge" >&2
    echo "  python3 -m venv .venv" >&2
    echo "  source .venv/bin/activate" >&2
    echo "  pip3 install -r scripts/discord_bot/requirements.txt" >&2
    exit 1
fi

mkdir -p "${LAUNCH_AGENTS_DIR}"
mkdir -p "${LOG_DIR}"

install_agent() {
    local label="$1"
    local plist="${label}.plist"
    local source_path="${SOURCE_DIR}/${plist}"
    local target_path="${LAUNCH_AGENTS_DIR}/${plist}"

    echo "Installing ${label}..."

    if [[ ! -f "${source_path}" ]]; then
        echo "  ERROR: source plist not found at ${source_path}" >&2
        return 1
    fi

    cp "${source_path}" "${target_path}"
    echo "  copied plist to ${target_path}"

    if launchctl print "gui/$(id -u)/${label}" >/dev/null 2>&1; then
        echo "  agent already loaded; unloading first..."
        launchctl bootout "gui/$(id -u)/${label}" || true
        # bootout returns immediately but launchd may still be reaping the
        # service — bootstrap'ing too fast yields "Bootstrap failed: 5: I/O
        # error". Give it a beat to clean up.
        sleep 2
    fi

    # Bootstrap with one retry on transient I/O errors. The race above is
    # the most common cause; a second bootstrap after a short sleep almost
    # always succeeds.
    if ! launchctl bootstrap "gui/$(id -u)" "${target_path}" 2>/dev/null; then
        echo "  first bootstrap failed (likely bootout race); retrying after 3s..."
        sleep 3
        launchctl bootstrap "gui/$(id -u)" "${target_path}"
    fi
    echo "  bootstrapped"

    # Some daemons (notably discord-bot, which spins up 9 bot clients) take
    # several seconds to fully come online. Give them time before checking.
    sleep 3
    if launchctl print "gui/$(id -u)/${label}" >/dev/null 2>&1; then
        echo "  status: loaded"
    else
        echo "  status: NOT LOADED" >&2
        return 1
    fi
}

failed=0
for label in "${AGENTS[@]}"; do
    if ! install_agent "${label}"; then
        failed=$((failed + 1))
    fi
    echo ""
done

if (( failed > 0 )); then
    echo "Installed with ${failed} failure(s). Check logs:" >&2
    echo "  ls ${LOG_DIR}/" >&2
    exit 1
fi

echo "Done."
echo ""
echo "Tail logs:"
echo "  tail -f ${LOG_DIR}/discord-bot.stderr.log"
echo "  tail -f ${LOG_DIR}/publish-prep.stderr.log"
echo "  tail -f ${LOG_DIR}/compile-nightly.stderr.log"
echo ""
echo "Manually fire a scheduled job (out-of-schedule test):"
echo "  launchctl kickstart gui/\$(id -u)/com.andyherman.neural-bridge.publish-prep"
echo "  launchctl kickstart gui/\$(id -u)/com.andyherman.neural-bridge.compile-nightly"
echo ""
echo "Uninstall with:"
echo "  ./scripts/launchd/uninstall.sh"
