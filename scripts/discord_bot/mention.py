"""Mention routing helpers — pure (no discord import) so they're testable.

When any Neural Bridge agent is @-mentioned in Discord, the daemon
spawns claude -p with that agent's plugin definition + conversation
context, and posts the response.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .claude_invoke import sanitize_untrusted_text
from . import honcho_client
from . import memory_telemetry as _mem

_logger = logging.getLogger("nb_discord.mention")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MENTION_PROMPT_PATH = PROMPTS_DIR / "mention_v1.md"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = REPO_ROOT / "plugins" / "neural-bridge-core" / "agents"

MAX_RESPONSE_CHARS = 5000
MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARS_PER_MESSAGE = 500

# Discord hard limit per message is 2000 chars. Leaving room for any
# trailing markdown safety, we chunk at 1900.
DISCORD_CHUNK_BUDGET = 1900

# Per-agent response cap override. Agents not listed fall back to MAX_RESPONSE_CHARS.
# Long values trigger chunking across multiple Discord messages (DISCORD_CHUNK_BUDGET each).
# Agents are still told (via the mention prompt) to target ~1500 chars; these caps are the
# truncation safety valve when an agent legitimately has more to say. Discord chunker handles
# the multi-message split. Cap > DISCORD_CHUNK_BUDGET means the response WILL be chunked.
#
# - research / teaching-prep / senior-pm / security-reviewer: sourced analysis can run long.
# - luna: 1:1 DM conversations need room; 11400 gives 6 chunks.
# - content / docs-editor / social: summaries and drafts routinely exceed the global default.
MAX_RESPONSE_CHARS_PER_AGENT: dict[str, int] = {
    "teaching-prep": 8000,
    "research": 8000,
    "senior-pm": 7000,
    "security-reviewer": 7000,
    "luna": 11400,
    "content": 5000,
    "social": 4000,
    "docs-editor": 5000,
    # Loid: 1:1 Telegram + DM conversations. Coach responses are typically
    # short (Loid listens more than he speaks), but session summaries and
    # handoff drafts can run long. Match Luna's ceiling.
    "loid": 11400,
}


def max_response_chars_for(agent_id: str) -> int:
    """Per-agent response cap, with a default."""
    return MAX_RESPONSE_CHARS_PER_AGENT.get(agent_id, MAX_RESPONSE_CHARS)


# Per-agent subprocess timeout (seconds). teaching-prep hits web + corpus reads
# that routinely run past the 300s global default; 600s gives it real headroom.
TIMEOUT_PER_AGENT: dict[str, int] = {
    "teaching-prep": 600,
    "recruiter": 480,  # charter write + create_agent action can exceed 300s default
    "content": 600,    # long-form drafts + multi-section summaries push past 300s
    "social": 480,     # voice-matching + multi-platform variants
}


def timeout_for(agent_id: str) -> int:
    """Per-agent claude -p timeout, with a default."""
    from .claude_invoke import DEFAULT_TIMEOUT
    return TIMEOUT_PER_AGENT.get(agent_id, DEFAULT_TIMEOUT)


# Per-agent reasoning depth (`--effort`). Claude 5-generation models think by
# default, so leaving this unset means a one-line Discord reply costs the same
# reasoning budget as a threat model. Match depth to the job:
#
#   low     conversational turns and routing — most of Luna's traffic
#   medium  drafting and editing where quality matters but the task is bounded
#   high    genuine multi-step analysis: research, security review, triage
#
# DEFAULT_EFFORT applies to anyone unlisted. Raise an individual agent rather
# than the default when its answers feel shallow; that keeps the cheap path
# cheap. xhigh/max are deliberately unused here — they belong to the loop
# engineer, which is doing real code generation.
DEFAULT_EFFORT = "medium"
EFFORT_PER_AGENT: dict[str, str] = {
    "luna": "low",             # assistant chatter, calendar/inbox lookups
    "echo": "low",             # voice-profile upkeep, pattern matching
    "librarian": "low",        # vault index lookups and audits
    "docs-editor": "medium",
    "content": "medium",
    "social": "medium",
    "ux-designer": "medium",
    "teaching-prep": "medium",
    "recruiter": "medium",
    "loid": "medium",
    "research": "high",        # multi-source synthesis
    "security-reviewer": "high",  # adversarial reasoning is the whole job
    "senior-pm": "high",       # dependency and priority reasoning across a board
    "automation-engineer": "high",
}


def effort_for(agent_id: str) -> str:
    """Per-agent reasoning depth, with a default."""
    return EFFORT_PER_AGENT.get(agent_id, DEFAULT_EFFORT)


# Per-agent extra read directories granted to claude -p via --add-dir. Used when
# an agent's source-of-truth lives outside the daemon's CWD (e.g., the INFO 310A
# corpus in the vault for the professor agent).
#
# Paths are absolute, expanded at module load. The daemon is responsible for
# ensuring these paths actually exist; if a directory is missing, claude -p
# will likely warn but continue.
INFO_310A_CORPUS = str(
    Path.home() / "Documents" / "Luna Master" / "Neural Bridge" / "Corpus" / "INFO 310A"
)
HUSKYHUB_LABS = str(Path.home() / "Development" / "huskyhub")
LUNA_VAULT = str(Path.home() / "Documents" / "Luna Master" / "Agents" / "Luna")
# Full Obsidian vault root — Luna gets read access to everything Andy has
# in his vault so she can stay current on his life: Seoul E-Land FC fan
# content (Sports/Seoul_E-Land), INFO 310 teaching schedule and lesson-plan
# corpus (Neural Bridge/Corpus/INFO 310A), Neural Bridge build journal,
# regulatory research, etc. The Agents/Luna/ subpath inside is where she writes
# her own notes (charter forbids writing anywhere else under the vault).
OBSIDIAN_VAULT_ROOT = str(Path.home() / "Documents" / "Luna Master")

# Loid (career strategist) needs read/write on the Synapse SQLite DB at
# ~/Development/Synapse/data/ (he reads via the synapse-journal CLI; he writes
# journal entries when Andy explicitly asks). He also needs his own vault
# folder at ~/Documents/Luna Master/Agents/Loid/ for Notes/Sessions/Ideas/Handoffs.
SYNAPSE_DATA_DIR = str(Path.home() / "Development" / "Synapse" / "data")
LOID_VAULT = str(Path.home() / "Documents" / "Luna Master" / "Agents" / "Loid")

ADD_DIRS_PER_AGENT: dict[str, list[str]] = {
    # Professor: read the corpus + the actual lab repo for end-to-end context.
    "teaching-prep": [INFO_310A_CORPUS, HUSKYHUB_LABS],
    # automation-engineer also benefits from huskyhub when reviewing lab code.
    "automation-engineer": [HUSKYHUB_LABS],
    # Luna: full Obsidian vault read access (Andy's entire life context —
    # Sports/Seoul_E-Land, Neural Bridge, INFO 310 teaching, regulatory
    # research, etc.) plus her own working-memory file. She writes ONLY
    # to Luna/notes.md per charter; the vault-root add-dir grants read
    # context that travels across all her conversations. Blog-repo read
    # access supports her `open_pr_with_changes` push rights — she needs
    # to Read existing files before deciding what to change. Scoped to
    # src/ + public/ to skip node_modules (~14k files makes claude do a
    # very slow startup inventory). For top-level files like
    # astro.config.mjs, ask Andy or @automation-engineer to surface them
    # — that's rare enough to not pay the scan cost on every mention.
    "luna": [
        OBSIDIAN_VAULT_ROOT,
        str(Path.home() / "Development" / "neural-bridge-blog" / "src"),
        str(Path.home() / "Development" / "neural-bridge-blog" / "public"),
    ],
    # Content + social: full vault read so they can pull style observations
    # from Andy Profile/, build journal context for technical pieces, the
    # Voice corpus for LinkedIn samples, etc. Charter constrains writes to
    # their own subdirs. (Phase 5 of the Echo build.) Content also gets
    # blog-repo read (scoped — see Luna's entry above for why) so she can
    # ship posts via `open_pr_with_changes`.
    "content": [
        OBSIDIAN_VAULT_ROOT,
        str(Path.home() / "Development" / "neural-bridge-blog" / "src"),
        str(Path.home() / "Development" / "neural-bridge-blog" / "public"),
    ],
    "social": [OBSIDIAN_VAULT_ROOT],
    # Echo: needs full vault read to do profile maintenance (reads Build
    # Journal, Drafts, Voice, etc.) AND voice authentication reviews (which
    # consume the Andy Profile/ files). Writes ONLY to Andy Profile/ per
    # charter; vault-root add-dir is for reads.
    "echo": [OBSIDIAN_VAULT_ROOT],
    # UX-designer: read access to the blog repo (Astro templates,
    # existing CSS, content frontmatter conventions) since that's the
    # primary surface she designs. Plus vault read for design rationale
    # docs and reference samples. Same node_modules-skip rationale as
    # Luna above — scoped to src/ + public/.
    "ux-designer": [
        str(Path.home() / "Development" / "neural-bridge-blog" / "src"),
        str(Path.home() / "Development" / "neural-bridge-blog" / "public"),
        OBSIDIAN_VAULT_ROOT,
    ],
    # Loid: his vault folder (write) + Synapse data dir (read/write via the
    # synapse-journal CLI invoked through Bash). Echo's Andy Profile/ falls
    # under OBSIDIAN_VAULT_ROOT so he can read it without explicit grant.
    "loid": [
        LOID_VAULT,
        SYNAPSE_DATA_DIR,
        OBSIDIAN_VAULT_ROOT,
    ],
}


# ----------- Luna's persistent memory: vault notes auto-injection -----------
#
# Luna's notes.md is her own working memory across Discord sessions. Every
# mention against Luna prefixes the rendered prompt with the current contents
# of that file, so anything she's written travels with her into the next
# conversation. She doesn't have to read it as a tool call — it's already in
# her context window when claude -p starts.

LUNA_NOTES_PATH = Path.home() / "Documents" / "Luna Master" / "Agents" / "Luna" / "notes.md"
LUNA_NOTES_MAX_CHARS = 8000  # bound prompt size; truncate-with-ellipsis otherwise

# Weekly lessons-learned digest (produced by scripts/summarize_weekly.py). Same
# auto-inject pattern as Luna's notes, but generalized: every agent that has a
# digest file gets it injected. The summarizer cron writes to
# Agents/<agent_id>/lessons-learned/YYYY-WW.md every Monday 04:00, compressing
# the prior 7 days of conversation logs into preferences + patterns + decisions
# the agent should carry into next week. We always inject the MOST RECENT file
# in that directory (one week behind at worst).
LESSONS_LEARNED_BASE = Path.home() / "Documents" / "Luna Master" / "Agents"
LESSONS_LEARNED_MAX_CHARS = 4000  # digest is supposed to be ~2k; cap with headroom


def _lessons_block(agent_id: str) -> str:
    """Read the most recent weekly lessons-learned digest for this agent and
    return a context block to prepend to the mention prompt. Empty string if
    no digest exists yet (new agent, or summarize_weekly hasn't run for them).

    The digest is the compressed memory pattern — what to carry into THIS
    week's conversations based on the prior week's signal. Distinct from
    Luna's manual notes.md (which only Luna maintains) and from the verbatim
    conversation archive (searched on-demand via Grep).
    """
    digest_dir = LESSONS_LEARNED_BASE / agent_id / "lessons-learned"
    if not digest_dir.exists():
        # This layer returned "" on every turn from May to August and nobody
        # noticed, because an empty string is indistinguishable from "no news".
        _mem.record(_mem.RETRIEVE, "lessons_digest", agent_id=agent_id,
                    ok=False, detail="no lessons-learned dir")
        return ""
    try:
        digests = sorted(digest_dir.glob("*.md"))
    except OSError as exc:
        _mem.record(_mem.RETRIEVE, "lessons_digest", agent_id=agent_id,
                    ok=False, detail=f"listdir failed: {exc}")
        return ""
    if not digests:
        _mem.record(_mem.RETRIEVE, "lessons_digest", agent_id=agent_id,
                    ok=False, detail="dir exists but holds no digests")
        return ""
    most_recent = digests[-1]
    try:
        content = most_recent.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _mem.record(_mem.RETRIEVE, "lessons_digest", agent_id=agent_id,
                    ok=False, detail=f"read failed: {exc}")
        return ""
    if not content.strip():
        _mem.record(_mem.RETRIEVE, "lessons_digest", agent_id=agent_id,
                    ok=False, detail=f"{most_recent.name} is empty")
        return ""
    _mem.record(_mem.RETRIEVE, "lessons_digest", agent_id=agent_id,
                ok=True, chars=len(content), detail=most_recent.name)
    if len(content) > LESSONS_LEARNED_MAX_CHARS:
        content = content[:LESSONS_LEARNED_MAX_CHARS].rstrip() + "\n\n[...truncated to fit prompt budget...]"
    sanitized = sanitize_untrusted_text(content, "lessons-learned")
    return (
        f"## Your lessons learned (auto-injected from "
        f"~/Documents/Luna Master/Agents/{agent_id}/lessons-learned/{most_recent.name})\n\n"
        f"This is a compressed digest of patterns, preferences, decisions, and "
        f"open threads from your conversations with Andy over the past week. "
        f"You wrote it via the weekly summarization cron. Treat it as your own "
        f"working memory; it is already in your context, so don't re-read the "
        f"file via a tool call. For verbatim past conversations, the full "
        f"archive is at `~/Documents/Luna Master/Agents/{agent_id}/conversations/`.\n\n"
        f"<lessons-learned>\n{sanitized}\n</lessons-learned>\n\n"
    )


# Headings whose content is a rolling log (a changelog of what happened). When
# the notes file overflows the injection budget, THIS is what gets dropped —
# never the durable sections above it.
#
# Why this exists: the original implementation kept the file's TAIL
# (`notes[-8000:]`). Luna's notes.md grew to ~16k chars with the curated
# material at the top (standing preferences, voice, and the "Decisions Andy has
# made that I should honor" list) and an append-only session log at the bottom.
# Tail-slicing therefore discarded her entire constitution every turn and kept
# the changelog — silently, for months. Durable-first is the correct priority:
# a rule Andy set is worth more than a record of which PR merged.
_LOG_HEADING_RE = re.compile(
    r"^#+\s*(session log|change ?log|activity log|log)\b", re.IGNORECASE
)
_TRUNC_MARK = "\n\n[…trimmed to fit prompt budget…]\n"


def split_note_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into [(heading, block)] where block includes its heading.

    Content before the first `##` heading is returned first with heading "".
    """
    sections: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            if buf:
                sections.append((heading, "".join(buf)))
            heading = line.strip()
            buf = [line]
        else:
            buf.append(line)
    if buf:
        sections.append((heading, "".join(buf)))
    return sections


def budget_notes(text: str, max_chars: int) -> tuple[str, list[str]]:
    """Fit a notes file into `max_chars`, dropping rolling-log sections before
    durable ones. Returns (kept_text, dropped_section_headings).

    Document order is preserved in the output. If the durable sections alone
    overflow the budget the log is dropped entirely and the durable content is
    cut from the end — that case means the curated notes need a human refactor,
    and the caller logs it loudly.
    """
    if len(text) <= max_chars:
        return text, []

    sections = split_note_sections(text)
    is_log = [_LOG_HEADING_RE.match(h) is not None for h, _ in sections]
    durable_total = sum(len(b) for (_, b), lg in zip(sections, is_log) if not lg)

    out: list[str] = []
    dropped: list[str] = []
    # When durable content fits, it is all kept and the remainder funds the log.
    # When it doesn't, the log gets nothing and durable is cut from the end.
    budget = max_chars - durable_total if durable_total <= max_chars else 0
    durable_budget = max_chars if durable_total > max_chars else None

    for (heading, block), lg in zip(sections, is_log):
        label = heading or "(preamble)"
        if lg:
            if budget <= 0:
                dropped.append(label)
            elif len(block) <= budget:
                out.append(block)
                budget -= len(block)
            else:
                # Keep the head of the log: newest entries first by convention.
                out.append(block[:budget].rstrip() + _TRUNC_MARK)
                dropped.append(f"{label} (partial)")
                budget = 0
            continue

        if durable_budget is None:
            out.append(block)
        elif durable_budget <= 0:
            dropped.append(label)
        elif len(block) <= durable_budget:
            out.append(block)
            durable_budget -= len(block)
        else:
            out.append(block[:durable_budget].rstrip() + _TRUNC_MARK)
            dropped.append(f"{label} (partial)")
            durable_budget = 0

    return "".join(out), dropped


def _luna_notes_block() -> str:
    """Read Luna's vault notes file and return a context block to prepend to
    her mention prompt. Empty string if the file is missing or unreadable.
    """
    if not LUNA_NOTES_PATH.exists():
        _mem.record(_mem.RETRIEVE, "luna_notes", agent_id="luna",
                    ok=False, detail="notes.md missing")
        return ""
    try:
        notes = LUNA_NOTES_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _mem.record(_mem.RETRIEVE, "luna_notes", agent_id="luna",
                    ok=False, detail=f"read failed: {exc}")
        return ""
    if not notes.strip():
        _mem.record(_mem.RETRIEVE, "luna_notes", agent_id="luna",
                    ok=False, detail="notes.md is empty")
        return ""
    raw_chars = len(notes)
    notes, dropped = budget_notes(notes, LUNA_NOTES_MAX_CHARS)
    if dropped:
        # Never fail silently: a shrinking memory is invisible from the outside.
        _logger.warning(
            "luna notes.md over budget (%d chars); dropped from prompt: %s",
            LUNA_NOTES_MAX_CHARS, ", ".join(dropped),
        )
    # Recorded as a successful retrieve that lost content, not as a failure:
    # she did get her notes, just not all of them. The canary watches the
    # dropped count separately so trimming does not read as an outage.
    _mem.record(_mem.RETRIEVE, "luna_notes", agent_id="luna", ok=True,
                chars=len(notes),
                detail=(f"trimmed {raw_chars}->{len(notes)}; dropped "
                        + "; ".join(dropped)) if dropped else "full")
    sanitized = sanitize_untrusted_text(notes, "luna-notes")
    return (
        "## Your prior notes (auto-injected from "
        "~/Documents/Luna Master/Agents/Luna/notes.md)\n\n"
        "These are notes you wrote in past sessions about Andy's preferences, "
        "voice, recurring commitments, open conversation threads, and decisions "
        "he's made. Read them as your own working memory; they're already in "
        "your context, so don't re-read the file via a tool call. When something "
        "new is worth remembering across sessions, append to notes.md during "
        "this session via Edit (the daemon grants you write access there).\n\n"
        f"<luna-notes>\n{sanitized}\n</luna-notes>\n\n"
    )


# ----------- Echo profile auto-inject (Phase 5) -----------
#
# Echo (the self-knowledge agent) maintains profile files at
# ~/Documents/Luna Master/Andy Profile/. The voice file in particular is
# what content / social / luna need pre-loaded when generating user-facing
# prose — having it in context means they don't need to fish for it via a
# tool call.
#
# vocabulary.md is left as on-demand Read; it's a reference table, not a
# rule set, and pre-loading it would burn budget for marginal benefit.

ECHO_VOICE_PATH = Path.home() / "Documents" / "Luna Master" / "Andy Profile" / "voice.md"
ECHO_VOICE_MAX_CHARS = 6000

# Agents that get Echo's voice profile auto-injected at the top of their prompt.
# Limit to voice-mirroring agents; agents with technical / structural roles
# (research, automation-engineer, security-reviewer, etc.) don't need it.
ECHO_VOICE_AGENTS = {"content", "social", "luna"}


def _echo_voice_block() -> str:
    """Read Echo's voice.md profile and return an auto-inject block.
    Empty string if missing/unreadable so the prompt builder degrades gracefully.
    """
    if not ECHO_VOICE_PATH.exists():
        _mem.record(_mem.RETRIEVE, "echo_voice", ok=False, detail="voice.md missing")
        return ""
    try:
        voice = ECHO_VOICE_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _mem.record(_mem.RETRIEVE, "echo_voice", ok=False, detail=f"read failed: {exc}")
        return ""
    if not voice.strip():
        _mem.record(_mem.RETRIEVE, "echo_voice", ok=False, detail="voice.md is empty")
        return ""
    _mem.record(_mem.RETRIEVE, "echo_voice", ok=True, chars=len(voice))
    if len(voice) > ECHO_VOICE_MAX_CHARS:
        voice = voice[: ECHO_VOICE_MAX_CHARS - 1].rstrip() + "\n[…profile truncated to fit prompt budget. Read the full file via the Read tool if needed: `~/Documents/Luna Master/Andy Profile/voice.md`]"
    sanitized = sanitize_untrusted_text(voice, "echo-voice")
    return (
        "## Andy's voice profile (auto-injected from Echo's `voice.md`)\n\n"
        "Echo (the self-knowledge agent) maintains a structured, citation-grounded "
        "profile of how Andy writes. The block below is the latest `voice.md`. Use "
        "it to mirror his voice when generating user-facing prose. Don't re-read "
        "the file via a tool call — it's already in your context. For deeper detail "
        "(vocabulary list, thinking patterns, opinions, raw examples), Read the "
        "sibling files at `~/Documents/Luna Master/Andy Profile/` on demand.\n\n"
        f"<echo-voice>\n{sanitized}\n</echo-voice>\n\n"
    )


def add_dirs_for(agent_id: str) -> list[str] | None:
    """Per-agent extra `--add-dir` paths for claude -p, or None if none configured.

    Two things are always added (each one idempotently, skipped if already
    covered by an existing parent path):

      1. The agent's own conversation-log archive
         (`Agents/<agent_id>/conversations/`).
      2. The shared cross-agent conversation archive
         (`Agents/_shared/conversations/`) — so when guild-channel threads
         involve multiple agents, each can see what the others said.

    For agents who already have the full vault root (luna, content, social,
    echo), both are covered automatically and no new entries are added.
    """
    from .conversation_log import agent_conversations_dir, shared_conversations_dir

    base = list(ADD_DIRS_PER_AGENT.get(agent_id, []))
    extras = [
        str(agent_conversations_dir(agent_id)),
        str(shared_conversations_dir()),
    ]

    for extra in extras:
        if not any(extra.startswith(d.rstrip("/") + "/") or extra == d for d in base):
            base.append(extra)

    return base if base else None


def chunk_for_discord(text: str, *, budget: int = DISCORD_CHUNK_BUDGET) -> list[str]:
    """Split a long response into Discord-postable chunks.

    Splits on natural boundaries in priority order:
      1. Double-newline (paragraph)
      2. Single-newline (line)
      3. Hard cut at budget

    Always returns at least one chunk. Each chunk is <= budget chars.
    Empty input returns an empty list.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= budget:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > budget:
        # Prefer paragraph break
        slice_end = remaining.rfind("\n\n", 0, budget)
        if slice_end == -1 or slice_end < budget // 2:
            # Fall back to line break
            slice_end = remaining.rfind("\n", 0, budget)
        if slice_end == -1 or slice_end < budget // 2:
            # Hard cut
            slice_end = budget
        chunk = remaining[:slice_end].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[slice_end:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


# Per-agent allowed_tools when responding to a Discord @-mention.
# PR-P-2: read-only PLUS Write + Edit so agents can take notes, save drafts,
# update prior notes. Each agent's plugin definition scopes WHERE they should
# write (to their own knowledge/agents/<id>/ subdir); the prompt enforces it.
# Bash is still excluded — agents cannot run shell commands or gh from
# mentions. Autonomous gh actions ship in a later PR via a structured
# tool-use protocol (agent emits intent JSON, daemon executes).
#
# security-reviewer is intentionally read-only (per its plugin: surfaces
# findings but never auto-applies fixes).
MENTION_ALLOWED_TOOLS: dict[str, str] = {
    "research":            "WebSearch,WebFetch,Read,Glob,Grep,Write,Edit",
    "teaching-prep":       "WebSearch,WebFetch,Read,Glob,Grep,Write,Edit",
    "content":             "WebSearch,WebFetch,Read,Glob,Grep,Write,Edit",
    "social":              "WebSearch,WebFetch,Read,Glob,Grep,Write,Edit",
    "recruiter":           "WebSearch,WebFetch,Read,Glob,Grep,Write,Edit",
    "automation-engineer": "Read,Glob,Grep,Write,Edit",  # no web; deals with local infra
    "security-reviewer":   "WebSearch,WebFetch,Read,Glob,Grep",  # read-only by design
    "docs-editor":         "WebSearch,WebFetch,Read,Glob,Grep,Write,Edit",
    "senior-pm":           "WebSearch,WebFetch,Read,Glob,Grep,Write,Edit",
    # Luna: executive assistant. General read/write for her notes file +
    # Calendar (read+write) and Gmail (read+draft) via the claude.ai MCP
    # connectors. List specific tool names because Claude Code's --allowedTools
    # doesn't support mcp__server__* wildcards. Add more entries here as Luna's
    # workflow surfaces new tool needs.
    "luna": (
        "WebSearch,WebFetch,Read,Glob,Grep,Write,Edit,"
        "mcp__claude_ai_Google_Calendar__authenticate,"
        "mcp__claude_ai_Google_Calendar__list_events,"
        "mcp__claude_ai_Google_Calendar__create_event,"
        "mcp__claude_ai_Google_Calendar__update_event,"
        "mcp__claude_ai_Google_Calendar__delete_event,"
        "mcp__claude_ai_Gmail__authenticate,"
        "mcp__claude_ai_Gmail__search_threads,"
        "mcp__claude_ai_Gmail__get_thread,"
        "mcp__claude_ai_Gmail__create_draft,"
        "mcp__claude_ai_Gmail__list_drafts,"
        "mcp__claude_ai_Gmail__list_labels,"
        # Google Drive (read-side only, mirroring the Gmail "no destructive
        # writes" pattern). Luna needs these to act as file-fetcher for the
        # squad per her Drive-overflow charter. If she needs to create/copy
        # files or change sharing permissions, surface to Andy or
        # @automation-engineer rather than wiring those tools here.
        "mcp__claude_ai_Google_Drive__search_files,"
        "mcp__claude_ai_Google_Drive__list_recent_files,"
        "mcp__claude_ai_Google_Drive__read_file_content,"
        "mcp__claude_ai_Google_Drive__download_file_content,"
        "mcp__claude_ai_Google_Drive__get_file_metadata,"
        "mcp__claude_ai_Google_Drive__get_file_permissions"
    ),
    # Librarian: Obsidian vault index + audits + restructure proposals.
    # Read/Write/Edit on the vault (which is mounted into knowledge/ via
    # symlink) plus Glob/Grep for navigation. No web, no MCP — pure
    # local-substrate work.
    "librarian":           "Read,Glob,Grep,Write,Edit",
    # Echo: Andy's voice-double. Profile maintenance + voice authentication
    # review of drafts. Pure local work — reads vault corpus + draft text,
    # writes profile files + structured review verdicts. No web needed
    # (her job is observation of Andy's own writing, not external research).
    "echo":                "Read,Glob,Grep,Write,Edit",
    # UX-designer: visual / interaction design for neural-bridge-blog and
    # other web surfaces. Web tools so she can fetch design references
    # (typography, palette examples, component patterns). Write/Edit for
    # CSS, Astro template fragments, and design rationale notes.
    "ux-designer":         "WebSearch,WebFetch,Read,Glob,Grep,Write,Edit",
    # Loid: career strategist. Read/Write/Edit on his vault folder + Glob/Grep
    # for navigation. Bash is required for the `synapse-journal` CLI (reads
    # Synapse SQLite, writes journal entries on Andy's explicit ask). No web
    # tools (he is a conversational strategist, not a researcher; market /
    # competitor research goes to @research).
    "loid":                "Read,Glob,Grep,Write,Edit,Bash",
}


def allowed_tools_for(agent_id: str) -> str | None:
    """Return the comma-separated --allowedTools value for this agent's
    Discord mentions, or None if the agent has no tool access in mention
    mode."""
    return MENTION_ALLOWED_TOOLS.get(agent_id)


def load_agent_definition(agent_id: str, agents_dir: Path = AGENTS_DIR) -> str:
    """Read plugins/neural-bridge-core/agents/<agent-id>.md and strip the
    YAML frontmatter. Returns the body — the agent's role definition,
    operating rules, voice, etc."""
    path = agents_dir / f"{agent_id}.md"
    if not path.exists():
        return f"_(agent definition not found at {path.name})_"
    text = path.read_text(encoding="utf-8")
    # Strip leading frontmatter
    if text.startswith("---"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5:]
    return text.strip()


def format_discord_history(messages: list[dict]) -> str:
    """Render a list of dicts (each with `author`, `content`) as a wrapped
    history block. Truncates per-message content to keep the prompt budgeted.

    Each dict shape:
      {"author": "<display name>", "content": "<message text>"}
    """
    if not messages:
        return "(no recent messages)"
    lines: list[str] = []
    for msg in messages[-MAX_HISTORY_MESSAGES:]:
        author = sanitize_untrusted_text(str(msg.get("author", "?")), "discord-history")
        content = str(msg.get("content", ""))
        if len(content) > MAX_HISTORY_CHARS_PER_MESSAGE:
            content = content[: MAX_HISTORY_CHARS_PER_MESSAGE - 1].rstrip() + "…"
        content = sanitize_untrusted_text(content, "discord-history")
        lines.append(f"[{author}] {content}")
    return "\n".join(lines)


def build_mention_prompt(
    template: str,
    *,
    agent_id: str,
    agent_definition: str,
    channel_kind: str,
    history: list[dict],
    message_content: str,
    conversation_log_path: str = "",
) -> str:
    history_block = format_discord_history(history)
    sanitized_message = sanitize_untrusted_text(message_content, "message")
    sanitized_definition = sanitize_untrusted_text(agent_definition, "agent-definition")
    rendered = (
        template
        .replace("{agent_id}", agent_id)
        .replace("{agent_definition}", sanitized_definition)
        .replace("{channel_kind}", channel_kind)
        .replace("{discord_history}", history_block)
        .replace("{message}", sanitized_message)
        .replace("{conversation_log_path}", conversation_log_path)
    )
    # Echo's voice profile auto-injected for voice-mirroring agents
    # (content, social, luna). Lets them reference Andy's voice without a
    # tool call. Phase 5 of the Echo build.
    if agent_id in ECHO_VOICE_AGENTS:
        echo_prefix = _echo_voice_block()
        if echo_prefix:
            rendered = echo_prefix + rendered

    # Luna gets her own working-memory file auto-injected on top of Echo's
    # voice profile. Echo gives her stylistic mirror; her notes give her
    # episodic memory across conversations.
    if agent_id == "luna":
        notes_prefix = _luna_notes_block()
        if notes_prefix:
            rendered = notes_prefix + rendered

    # Every agent: prepend the most recent weekly lessons-learned digest if
    # one exists. This is the compressed compounding memory — last week's
    # preferences/decisions/patterns folded into a few hundred lines.
    # Produced by scripts/summarize_weekly.py on Monday 04:00. Layered ABOVE
    # Luna's notes (which are most-recent-first hand-curated) and Echo's
    # voice (style mirror) so the digest reads as the first thing in context.
    lessons_prefix = _lessons_block(agent_id)
    if lessons_prefix:
        rendered = lessons_prefix + rendered

    # Every agent: prepend the Honcho peer card — LLM-extracted persistent
    # observations about Andy, shared across all agents in this workspace and
    # with Yor (the Hermes-side thinking-partner agent). Each agent contributes
    # observations from its own perspective via directional mode; the peer-card
    # facts compound. Layered ABOVE lessons so it reads as the foundational
    # "who is this person" context before the more recent reflective digest.
    # No-ops if Honcho is unreachable (see honcho_client._enabled / _get_client).
    honcho_prefix = honcho_client.get_peer_card_context(agent_id)
    if honcho_prefix:
        rendered = honcho_prefix + rendered

    return rendered


def truncate_response(text: str, *, limit: int = MAX_RESPONSE_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def is_mention_for_self(message_mentions: list, my_user) -> bool:
    """Return True if this message @-mentions me (and not just everyone-mentions
    or other bots). Discord.py message.mentions is a list of User objects."""
    if my_user is None:
        return False
    return any(getattr(u, "id", None) == getattr(my_user, "id", None) for u in message_mentions)
