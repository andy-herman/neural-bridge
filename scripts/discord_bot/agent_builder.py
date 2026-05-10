"""create_agent action implementation.

When a recruiter (or any agent in the action allowlist) emits a
`create_agent` action, this module:

1. Validates the agent_id is unique (no collision with existing agents).
2. Writes the plugin file at `plugins/neural-bridge-core/agents/<id>.md`.
3. Adds the agent_id to `KNOWN_AGENTS` in `hooks/session_end.py` and
   `hooks/schema.py`.
4. Bumps the plugin version (minor) in `.claude-plugin/marketplace.json`
   and `plugins/neural-bridge-core/.claude-plugin/plugin.json`.
5. Commits to a feature branch, pushes, opens a PR.

All operations are idempotent at the per-step level — if a step has
already run (file exists, KNOWN_AGENTS already contains the id, etc.)
it short-circuits cleanly.

The Discord-side work (create application, store token, invite bot,
update agents.json) stays manual — those steps involve secrets.
"""

from __future__ import annotations

import json
import re
import subprocess as sp
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = REPO_ROOT / "plugins" / "neural-bridge-core" / "agents"
SESSION_END = REPO_ROOT / "hooks" / "session_end.py"
SCHEMA_PY = REPO_ROOT / "hooks" / "schema.py"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_JSON = REPO_ROOT / "plugins" / "neural-bridge-core" / ".claude-plugin" / "plugin.json"

VALID_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
VALID_COLOR = {"red", "orange", "yellow", "green", "blue", "purple", "cyan", "pink", "white", "magenta"}
VALID_TOOL = {
    "Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebSearch", "WebFetch",
    "NotebookEdit",
}


@dataclass
class CreateAgentResult:
    ok: bool
    agent_id: str
    branch: str | None = None
    pr_url: str | None = None
    file_written: Path | None = None
    error: str | None = None
    skipped_reasons: list[str] | None = None


# ---------- Validation ----------

def validate_create_agent_payload(action: dict) -> tuple[bool, str | None]:
    """Validate a create_agent action's shape (caller of execute_create_agent)."""
    if not isinstance(action, dict):
        return False, "action must be an object"
    required = {"agent_id", "display_name", "description", "color", "tools", "model", "body"}
    missing = required - set(action.keys())
    if missing:
        return False, f"missing keys: {sorted(missing)}"

    if not isinstance(action["agent_id"], str) or not VALID_AGENT_ID_RE.match(action["agent_id"]):
        return False, f"agent_id must be kebab-case: {action.get('agent_id')!r}"

    if not isinstance(action["display_name"], str) or not action["display_name"].strip():
        return False, "display_name must be non-empty string"

    if not isinstance(action["description"], str) or not action["description"].strip():
        return False, "description must be non-empty string"

    if action["color"] not in VALID_COLOR:
        return False, f"color must be one of {sorted(VALID_COLOR)} (got {action['color']!r})"

    tools = action["tools"]
    if not isinstance(tools, list) or not tools:
        return False, "tools must be a non-empty list"
    for t in tools:
        if t not in VALID_TOOL:
            return False, f"unknown tool: {t!r} (valid: {sorted(VALID_TOOL)})"

    if not isinstance(action["model"], str) or not action["model"].strip():
        return False, "model must be non-empty string"

    if not isinstance(action["body"], str) or len(action["body"].strip()) < 100:
        return False, "body must be a non-trivial markdown string (>= 100 chars)"

    return True, None


# ---------- Helpers ----------

def _agent_exists(agent_id: str, agents_dir: Path = AGENTS_DIR) -> bool:
    return (agents_dir / f"{agent_id}.md").exists()


def render_plugin_file(action: dict) -> str:
    """Build the full .md file content (frontmatter + body)."""
    tools_str = ", ".join(action["tools"])
    frontmatter = (
        "---\n"
        f"description: {action['description']}\n"
        f"tools: [{tools_str}]\n"
        f"model: {action['model']}\n"
        f"color: {action['color']}\n"
        "---\n\n"
    )
    return frontmatter + action["body"].strip() + "\n"


def update_known_agents(file_path: Path, agent_id: str) -> bool:
    """Add agent_id to the KNOWN_AGENTS literal in file_path. Returns True
    if the file changed; False if agent_id was already present."""
    text = file_path.read_text(encoding="utf-8")
    # Look for the KNOWN_AGENTS = { ... } block and check if agent_id is in it.
    block_re = re.compile(
        r"KNOWN_AGENTS\s*=\s*\{(?P<body>.*?)\}",
        re.DOTALL,
    )
    m = block_re.search(text)
    if not m:
        raise ValueError(f"KNOWN_AGENTS not found in {file_path}")
    block_body = m.group("body")
    if f'"{agent_id}"' in block_body:
        return False  # already present
    # Insert before the closing brace; preserve indentation by inserting on
    # a new line aligned with the existing entries.
    # Simple strategy: append `"<id>",\n    ` before the last entry that
    # ends in `,` (or before the closing brace if no trailing comma).
    new_body = block_body.rstrip()
    if new_body.endswith(","):
        new_body = new_body + f'\n    "{agent_id}",'
    else:
        new_body = new_body + f',\n    "{agent_id}",'
    new_text = text[: m.start("body")] + new_body + text[m.end("body"):]
    file_path.write_text(new_text, encoding="utf-8")
    return True


def bump_minor_version(json_path: Path, version_key_path: list[str]) -> str | None:
    """Bump the minor version in a JSON file. version_key_path is the path
    to the version field (e.g., ['plugins', 0, 'version']). Returns the
    new version string, or None if the field wasn't found."""
    text = json_path.read_text(encoding="utf-8")
    data = json.loads(text)
    cursor = data
    for key in version_key_path[:-1]:
        cursor = cursor[key]
    last = version_key_path[-1]
    current = cursor[last]
    parts = current.split(".")
    if len(parts) != 3:
        return None
    parts[1] = str(int(parts[1]) + 1)
    parts[2] = "0"
    new_version = ".".join(parts)
    cursor[last] = new_version
    json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return new_version


# ---------- Git ops ----------

def _git(args: list[str], cwd: Path = REPO_ROOT, timeout: int = 30) -> tuple[bool, str]:
    """Run a git command. Returns (ok, stderr-or-stdout-snippet)."""
    try:
        proc = sp.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=sp.DEVNULL,
        )
    except (sp.TimeoutExpired, FileNotFoundError) as exc:
        return False, f"{type(exc).__name__}"
    if proc.returncode != 0:
        snippet = (proc.stderr or proc.stdout or "")[:300].replace("\n", " ")
        return False, f"git_exit_{proc.returncode}: {snippet}"
    return True, proc.stdout.strip()


def _gh(args: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        proc = sp.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=sp.DEVNULL,
        )
    except (sp.TimeoutExpired, FileNotFoundError) as exc:
        return False, f"{type(exc).__name__}"
    if proc.returncode != 0:
        snippet = (proc.stderr or "")[:300].replace("\n", " ")
        return False, f"gh_exit_{proc.returncode}: {snippet}"
    return True, proc.stdout.strip()


# ---------- Top-level execution ----------

def execute_create_agent(action: dict, repo: str) -> CreateAgentResult:
    """Run the full create_agent workflow. Returns a CreateAgentResult.

    Side effects: writes plugin file, edits KNOWN_AGENTS in two places,
    bumps version in two JSON files, git commit + push, opens a PR.
    Idempotent at the per-step level.
    """
    ok, err = validate_create_agent_payload(action)
    if not ok:
        return CreateAgentResult(ok=False, agent_id=action.get("agent_id", "<unknown>"), error=err)

    agent_id = action["agent_id"]
    skipped: list[str] = []

    # Pre-check: is the agent already in the repo?
    if _agent_exists(agent_id):
        return CreateAgentResult(
            ok=False, agent_id=agent_id,
            error=f"agent file already exists at plugins/neural-bridge-core/agents/{agent_id}.md",
        )

    # Branch off main.
    branch_name = f"feat/agent-{agent_id}"
    ok, _ = _git(["checkout", "main"])
    if not ok:
        return CreateAgentResult(ok=False, agent_id=agent_id, error="git checkout main failed")
    ok, _ = _git(["pull", "--ff-only"])
    if not ok:
        skipped.append("git pull failed (continuing)")
    ok, msg = _git(["checkout", "-b", branch_name])
    if not ok:
        return CreateAgentResult(ok=False, agent_id=agent_id, error=f"branch create failed: {msg}")

    # Write the plugin file.
    target = AGENTS_DIR / f"{agent_id}.md"
    target.write_text(render_plugin_file(action), encoding="utf-8")

    # Update KNOWN_AGENTS in both places.
    try:
        if update_known_agents(SESSION_END, agent_id):
            pass
        else:
            skipped.append(f"hooks/session_end.py: KNOWN_AGENTS already had {agent_id}")
        if update_known_agents(SCHEMA_PY, agent_id):
            pass
        else:
            skipped.append(f"hooks/schema.py: KNOWN_AGENTS already had {agent_id}")
    except ValueError as exc:
        return CreateAgentResult(ok=False, agent_id=agent_id, branch=branch_name,
                                 error=f"KNOWN_AGENTS update failed: {exc}")

    # Bump plugin version (minor).
    new_version_marketplace = bump_minor_version(MARKETPLACE_JSON, ["plugins", 0, "version"])
    new_version_plugin = bump_minor_version(PLUGIN_JSON, ["version"])
    if not new_version_marketplace or not new_version_plugin:
        skipped.append("version bump failed (non-semver?)")

    # git add / commit / push.
    ok, msg = _git(["add", "-A"])
    if not ok:
        return CreateAgentResult(ok=False, agent_id=agent_id, branch=branch_name, error=f"git add failed: {msg}")
    commit_msg = (
        f"feat(agent): add {agent_id} specialist (created by recruiter)\n\n"
        f"display_name: {action['display_name']}\n"
        f"description: {action['description']}\n\n"
        f"Plugin file at plugins/neural-bridge-core/agents/{agent_id}.md.\n"
        f"KNOWN_AGENTS updated in hooks/session_end.py and hooks/schema.py.\n"
        f"Plugin version bumped (marketplace.json + plugin.json).\n\n"
        f"Manual steps remaining for Andy:\n"
        f"1. Create Discord application 'NB {action['display_name']}' in Developer Portal\n"
        f"2. Enable Message Content Intent on its Bot tab\n"
        f"3. Reset Token and store: security add-generic-password "
        f"-s 'neural-bridge-discord-bot-{agent_id}' -a \"$USER\" -w \"$(pbpaste)\"\n"
        f"4. Generate invite URL with the new client_id and authorize into the server\n"
        f"5. Add the new client_id to scripts/discord_bot/agents.json\n"
        f"6. Reload daemon: ./scripts/launchd/install.sh"
    )
    ok, msg = _git(["commit", "-m", commit_msg])
    if not ok:
        return CreateAgentResult(ok=False, agent_id=agent_id, branch=branch_name, error=f"git commit failed: {msg}")
    ok, msg = _git(["push", "-u", "origin", branch_name])
    if not ok:
        return CreateAgentResult(ok=False, agent_id=agent_id, branch=branch_name, error=f"git push failed: {msg}")

    # Open the PR.
    pr_body = (
        f"## Recruiter-driven agent rollout: `{agent_id}`\n\n"
        f"**Display name:** {action['display_name']}\n"
        f"**Description:** {action['description']}\n\n"
        f"## What was built\n\n"
        f"- Plugin file at `plugins/neural-bridge-core/agents/{agent_id}.md`\n"
        f"- `KNOWN_AGENTS` updated in `hooks/session_end.py` and `hooks/schema.py`\n"
        f"- Plugin version bumped (`marketplace.json` and `plugins/neural-bridge-core/.claude-plugin/plugin.json`)\n"
        f"\n## Manual steps still required (Andy)\n\n"
        f"1. **Discord application:** Developer Portal → New Application → name `NB {action['display_name']}` → Bot tab → enable **Message Content Intent** → Reset Token, copy.\n"
        f"2. **Store token:** `security add-generic-password -s \"neural-bridge-discord-bot-{agent_id}\" -a \"$USER\" -w \"$(pbpaste)\"`\n"
        f"3. **Invite bot:** Generate URL with the new client_id at `https://discord.com/oauth2/authorize?client_id=<NEW>&scope=bot+applications.commands&permissions=380104608832` → open → authorize.\n"
        f"4. **Update agents.json:** Add the new agent's client_id to `scripts/discord_bot/agents.json` (or ask the daemon).\n"
        f"5. **Reload:** `./scripts/launchd/install.sh`\n"
        f"6. **Smoke test:** `@{agent_id} ...` in `#neural-bridge`."
    )
    ok, msg = _gh(["pr", "create", "--title", f"feat(agent): add {agent_id} specialist", "--body", pr_body])
    if not ok:
        return CreateAgentResult(ok=False, agent_id=agent_id, branch=branch_name, error=f"gh pr create failed: {msg}")
    pr_url = msg.strip()

    # Return to main.
    _git(["checkout", "main"])

    return CreateAgentResult(
        ok=True, agent_id=agent_id, branch=branch_name, pr_url=pr_url,
        file_written=target, skipped_reasons=skipped or None,
    )
