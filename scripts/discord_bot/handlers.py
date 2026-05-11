"""Slash command handlers + thread message handler for senior-pm.

PR-I-A wires real PM intake → GitHub issue creation. The other slash
commands (/pm-summary, /squad-discuss, /triage, /close) remain stubs;
they get real logic in PR-K.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import discord

import json

from .auth import REFUSAL_MESSAGE, is_authorized
from .claude_invoke import call_claude
from .client_registry import post_as_agent
from .config import BotConfig
from .actions import extract_actions, validate_action_batch
from .agent_builder import execute_create_agent
from .attachments import extract_attachments, validate_attachment_batch
from .attachment_ingest import (
    ALLOWED_EXTENSIONS_PER_AGENT as INGEST_ALLOWED_EXTENSIONS_PER_AGENT,
    allowed_extensions_for as ingest_allowed_extensions_for,
    format_prompt_block as ingest_format_prompt_block,
    ingest_attachments,
)
from .pr_proposals import (
    STORE as PR_STORE,
    execute_proposal,
    format_preview as format_pr_preview,
    is_approval_text,
    is_cancel_text,
    validate_open_pr_action,
)
from .session_store import STORE as SESSION_STORE
from .client_registry import REGISTRY as CLIENT_REGISTRY
from .github_client import close_issue, comment_issue, create_issue, edit_issue_body
from .handoff_budget import BUDGET as HANDOFF_BUDGET
from .state_machine import add_label as add_label_async, remove_label as remove_label_async
from .obsidian_writer import ObsidianWriter
from .pm_intake import PMIntake, SessionState
from .state_machine import STATE_LABEL_SET, apply_labels
from .thread_map import ThreadMap
from .mention import (
    MENTION_PROMPT_PATH,
    add_dirs_for,
    allowed_tools_for,
    build_mention_prompt,
    chunk_for_discord,
    is_mention_for_self,
    load_agent_definition,
    max_response_chars_for,
    timeout_for,
    truncate_response,
)
from .squad_discuss import (
    FRAMING_PROMPT_PATH,
    TURN_PROMPT_PATH,
    build_framing_prompt,
    build_turn_prompt,
    truncate_framing,
    truncate_turn,
    validate_framing_output,
)
from .summary import (
    PROMPT_PATH as SUMMARY_PROMPT_PATH,
    build_summary_prompt,
    list_open_issues,
    truncate_for_discord,
)
from .triage import (
    TRIAGE_PROMPT_PATH,
    apply_auto_fixes,
    build_triage_prompt,
    fetch_issue,
    strip_code_fences,
    validate_triage_output,
)


_logger = logging.getLogger("nb_discord")


def log(msg: str) -> None:
    """Route through Python logging.

    Output goes to two places:
      - the rotating file at ~/Library/Logs/neural-bridge/discord-bot.log
        (configured in main._configure_logging — 10MB cap, 7 backups)
      - stderr (which launchd captures into discord-bot.stderr.log for
        boot-time diagnostics; that file is small and doesn't need rotation
        because most volume goes to the rotating .log instead)

    If the logger has no handlers (e.g., handlers.py is imported by tests
    without main.py setting up the daemon logger), we fall back to a
    print to stderr so test output isn't swallowed.
    """
    if _logger.handlers:
        _logger.info(msg)
    else:
        print(f"[discord_bot] {msg}", file=sys.stderr, flush=True)


# Singleton state for the daemon process.
INTAKE = PMIntake()
THREAD_MAP = ThreadMap()
VAULT = ObsidianWriter()


async def _gate(interaction: discord.Interaction, config: BotConfig, command: str) -> bool:
    user_id = str(interaction.user.id)
    if not is_authorized(user_id, config):
        log(f"REFUSED: /{command} from user_id={user_id} (not in authorized list)")
        await interaction.response.send_message(REFUSAL_MESSAGE, ephemeral=True)
        return False
    log(f"ACCEPTED: /{command} from user_id={user_id}")
    return True


def _truncate_thread_name(text: str, max_len: int = 90) -> str:
    text = " ".join(text.split())
    if len(text) <= max_len:
        return f"pm: {text}" if not text.lower().startswith("pm:") else text
    return f"pm: {text[: max_len - 4]}…"


# ---------- /pm-task: real handler ----------

async def handle_pm_task(interaction: discord.Interaction, config: BotConfig, request: str) -> None:
    if not await _gate(interaction, config, "pm-task"):
        return

    request = request.strip()
    if not request:
        await interaction.response.send_message(
            "Empty request — give me a sentence to work with.", ephemeral=True
        )
        return

    channel = interaction.channel
    if channel is None or not hasattr(channel, "create_thread"):
        await interaction.response.send_message(
            "Run /pm-task from a text channel that supports threads (e.g., #neural-bridge).",
            ephemeral=True,
        )
        return

    # Acknowledge first so we don't time out the interaction (3s budget).
    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        thread = await channel.create_thread(  # type: ignore[union-attr]
            name=_truncate_thread_name(request),
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,  # 7 days
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to create threads in this channel. "
            "Check the bot's role permissions.",
            ephemeral=True,
        )
        return

    session, question = INTAKE.start_session(
        thread_id=str(thread.id),
        user_id=str(interaction.user.id),
        request=request,
    )
    log(f"INTAKE start: thread={thread.id} user={interaction.user.id} request={request[:60]!r}")

    await thread.send(
        f"**PM intake** for: _{request[:200]}_\n\n{question}\n\n"
        f"_Reply in this thread. Say `cancel` to drop this task._"
    )
    await interaction.followup.send(
        f"Started clarification thread: <#{thread.id}>", ephemeral=True
    )


# ---------- on_mention: any agent answers when @-mentioned (PR-P-1) ----------

def _is_handoff_eligible(message, config: BotConfig) -> tuple[bool, str | None]:
    """Decide whether a message is allowed to invoke a mention handler.

    Returns (eligible, reason_to_skip).
    - Authorized human (Andy) → always eligible; resets the budget.
    - One of our own bots → eligible if budget allows; consumes one turn.
    - Anything else (other bots, unauthorized humans) → skip silently.
    """
    user_id = str(message.author.id)

    # Authorized human → reset budget, always eligible.
    if not message.author.bot and is_authorized(user_id, config):
        HANDOFF_BUDGET.reset(str(message.channel.id))
        return True, None

    # Bot author: only allow if it's one of OUR bots and budget remains.
    if message.author.bot:
        is_ours = any(
            getattr(c, "user", None) is not None
            and getattr(c.user, "id", None) == message.author.id
            for c in CLIENT_REGISTRY._by_id.values()
        )
        if not is_ours:
            return False, None
        if not HANDOFF_BUDGET.consume(str(message.channel.id)):
            return False, "handoff_budget_exhausted"
        return True, None

    # Unauthorized human, or anything else.
    return False, None


async def handle_mention(client, message, config: BotConfig) -> None:
    """Called from any AgentClient's on_message when this bot is @-mentioned.

    Eligibility:
      - Andy → always allowed; resets the per-channel handoff budget.
      - Another Neural Bridge bot → allowed if budget remains; consumes one
        turn. Cap default 5 turns/channel; reset on next Andy message.
      - Other bots / unauthorized humans → ignored silently.
    """
    eligible, skip_reason = _is_handoff_eligible(message, config)
    if not eligible:
        if skip_reason == "handoff_budget_exhausted":
            log(f"MENTION skip: handoff budget exhausted in channel={message.channel.id}")
            try:
                await message.channel.send(
                    "_(Handoff budget exhausted on this thread. "
                    "@-mention me directly to continue, or wait for Andy.)_"
                )
            except Exception:
                pass
        return

    agent_id = client.agent.id
    user_id = str(message.author.id)

    if not MENTION_PROMPT_PATH.exists():
        await message.channel.send(f"_(internal: mention prompt missing at {MENTION_PROMPT_PATH})_")
        return

    log(f"MENTION: agent={agent_id} channel={message.channel.id} from={user_id}")

    # Acknowledge so Andy sees the bot is thinking. Discord shows typing for ~10s
    # automatically when we use typing(); use it as a thinking indicator.
    async with message.channel.typing():
        # Inbound attachment ingest — agents in INGEST_ALLOWED_EXTENSIONS_PER_AGENT
        # are wired for this. Echo gets text/doc corpus (.txt/.md/.pdf/.eml/.docx).
        # Luna also gets images (.png/.jpg/etc.) because she's the assistant Andy
        # screenshots things to. Each agent's allowed-extensions set and drop dir
        # are defined in attachment_ingest.py.
        ingest_block = ""
        agent_allowed_exts = ingest_allowed_extensions_for(agent_id)
        if agent_allowed_exts and getattr(message, "attachments", None):
            relevant = [
                a for a in message.attachments
                if Path(getattr(a, "filename", "") or "").suffix.lower() in agent_allowed_exts
            ]
            if relevant:
                try:
                    ingest_result = await ingest_attachments(message, agent_id=agent_id)
                except Exception as exc:
                    log(f"MENTION ingest CRASHED (non-fatal): agent={agent_id} {type(exc).__name__}: {exc}")
                    ingest_result = None

                if ingest_result is not None:
                    if ingest_result.ingested:
                        ingest_block = ingest_format_prompt_block(ingest_result)
                        log(
                            f"MENTION ingest ok: agent={agent_id} "
                            f"files={len(ingest_result.ingested)} "
                            f"rejected={len(ingest_result.rejected)} "
                            f"over_cap={ingest_result.over_cap}"
                        )
                    # Surface rejections / over-cap as their own message so Andy
                    # knows what didn't make it through. Echo's prompt won't see
                    # rejected files, so they wouldn't otherwise be visible.
                    if ingest_result.rejected or ingest_result.over_cap:
                        lines = [f"_(Attachment ingest issues for `{agent_id}`:)_"]
                        for name, reason in ingest_result.rejected:
                            short = name if len(name) <= 80 else "…" + name[-77:]
                            lines.append(f"- ⚠️ `{short}` skipped: `{reason}`")
                        if ingest_result.over_cap:
                            lines.append(
                                f"- ⚠️ More than {5} files attached; only the first 5 were considered."
                            )
                        try:
                            await message.channel.send("\n".join(lines)[:1900])
                        except Exception as exc:
                            log(f"MENTION ingest notice send failed (non-fatal): {type(exc).__name__}: {exc}")

        # Fetch recent context. discord.py: history(limit=N).
        history: list[dict] = []
        try:
            async for h_msg in message.channel.history(limit=50, before=message):
                history.append({
                    "author": getattr(h_msg.author, "display_name", str(h_msg.author)),
                    "content": h_msg.content,
                })
            history.reverse()  # oldest -> newest
        except Exception as exc:
            log(f"MENTION history fetch failed (non-fatal): {type(exc).__name__}: {exc}")

        # Build prompt
        template = MENTION_PROMPT_PATH.read_text(encoding="utf-8")
        agent_definition = load_agent_definition(agent_id)
        channel_kind = "thread" if isinstance(message.channel, discord.Thread) else "channel"

        prompt = build_mention_prompt(
            template,
            agent_id=agent_id,
            agent_definition=agent_definition,
            channel_kind=channel_kind,
            history=history,
            message_content=message.content,
        )
        # Prepend the ingest block so Echo sees the dropped-files section before
        # the standard mention scaffold. Same composition pattern as the Echo
        # voice and Luna notes blocks (which are prepended inside build_mention_prompt).
        if ingest_block:
            prompt = ingest_block + prompt

        tools = allowed_tools_for(agent_id)
        extra_dirs = add_dirs_for(agent_id)
        agent_timeout = timeout_for(agent_id)

        # Per (channel × agent) session resumption. First mention creates a
        # fresh UUID and passes it via --session-id; subsequent mentions
        # resume via --resume <uuid>, so the model has the full prior
        # transcript including file Reads. Sessions persist across daemon
        # restarts (JSON-backed) and TTL out after 7 days inactive.
        session_rec, is_new_session = SESSION_STORE.get_or_create(
            int(message.channel.id), agent_id,
        )

        log(
            f"MENTION calling claude: agent={agent_id} tools={tools or 'none'} "
            f"add_dirs={len(extra_dirs) if extra_dirs else 0} timeout={agent_timeout}s "
            f"session={session_rec.session_id[:8]}... "
            f"({'new' if is_new_session else f'turn {session_rec.turn_count + 1}'})"
        )
        ok, stdout, err = await call_claude(
            prompt, timeout=agent_timeout, allowed_tools=tools, add_dirs=extra_dirs,
            session_id=session_rec.session_id,
            resume=not is_new_session,
        )

        # If --resume failed (most likely cause: Claude Code's own session
        # cleanup pruned the underlying file), retry ONCE with a fresh
        # session ID. We don't loop further — a second failure is a real
        # problem the user should know about.
        if not ok and not is_new_session:
            log(
                f"MENTION resume failed (will retry fresh): agent={agent_id} "
                f"old_session={session_rec.session_id[:8]}... err={err[:80]}"
            )
            session_rec = SESSION_STORE.reset(int(message.channel.id), agent_id)
            ok, stdout, err = await call_claude(
                prompt, timeout=agent_timeout, allowed_tools=tools, add_dirs=extra_dirs,
                session_id=session_rec.session_id,
                resume=False,
            )

        if ok:
            SESSION_STORE.touch(int(message.channel.id), agent_id)

    if not ok:
        log(f"MENTION claude FAILED: agent={agent_id} error={err}")
        await message.channel.send(
            f"_(I hit an error: `{err}`. Try again, or @-mention senior-pm to escalate.)_"
        )
        return

    # Extract any structured action block before truncating + posting.
    parsed = extract_actions(stdout)
    # Then extract any attachments block from what's left (order matters — both
    # are stripped from visible response before chunk + post).
    parsed_attach = extract_attachments(parsed.visible_response)
    response_cap = max_response_chars_for(agent_id)
    response = truncate_response(parsed_attach.visible_response, limit=response_cap)

    # Validate attachments up front so we can attach valid ones to the last
    # chunk and surface a fallback line for any rejected paths.
    valid_files: list = []
    attach_errors: list[tuple[str, str]] = []
    over_cap = False
    if parsed_attach.paths:
        validated = validate_attachment_batch(parsed_attach.paths)
        valid_files = validated.valid_paths
        attach_errors = validated.errors
        over_cap = validated.over_cap

    if not response and not parsed.actions and not parsed.parse_error and not valid_files:
        log(f"MENTION empty response: agent={agent_id}")
        await message.channel.send("_(I had nothing useful to add. Try rephrasing or @-mention a different specialist.)_")
        return

    if response:
        # Chunk into multiple messages if the response exceeds Discord's per-message limit.
        # Files (if any) attach to the last chunk so they appear after the prose context.
        # `discord` comes from the module-level import (line 12) — adding a local
        # `import discord` here makes Python treat the name as local for the entire
        # function, breaking the earlier `discord.Thread` reference at line 252
        # (UnboundLocalError on every mention).
        chunks = chunk_for_discord(response)
        for i, chunk in enumerate(chunks):
            # Tag EVERY chunk when there's a continuation, including part 1 — otherwise users
            # don't know more is coming and stop reading at the end of the first message.
            if len(chunks) > 1:
                chunk = f"_(part {i + 1}/{len(chunks)})_\n{chunk}"
            is_last = (i == len(chunks) - 1)
            if is_last and valid_files:
                files = [discord.File(str(p)) for p in valid_files]
                await message.channel.send(chunk, files=files)
            else:
                await message.channel.send(chunk)
    elif valid_files:
        # No prose response, just files — send them on their own.
        files = [discord.File(str(p)) for p in valid_files]
        await message.channel.send(files=files)

    # Attachment validation feedback (rejected paths + over-cap).
    if parsed_attach.parse_error:
        await message.channel.send(
            f"_(I tried to emit an attachments block but it didn't parse: `{parsed_attach.parse_error}`. "
            f"No files were attached.)_"
        )
        log(f"MENTION attachments parse FAILED: agent={agent_id} error={parsed_attach.parse_error}")
    elif attach_errors or over_cap:
        lines = [f"_(Attachment issues from `{agent_id}`:)_"]
        for path, reason in attach_errors:
            short = path if len(path) <= 80 else "…" + path[-77:]
            lines.append(f"- ⚠️ `{short}` rejected: `{reason}`")
        if over_cap:
            lines.append(f"- ⚠️ More than {5} attachments requested; only the first 5 were considered.")
        await message.channel.send("\n".join(lines)[:1900])
        log(f"MENTION attachments rejected: agent={agent_id} count={len(attach_errors)} over_cap={over_cap}")

    # Action block handling.
    if parsed.parse_error:
        await message.channel.send(
            f"_(I tried to emit an action block but it didn't parse: `{parsed.parse_error}`. "
            f"No actions were taken.)_"
        )
        log(f"MENTION action parse FAILED: agent={agent_id} error={parsed.parse_error}")
    elif parsed.actions:
        ok, err, valid = validate_action_batch(parsed.actions)
        if not ok:
            await message.channel.send(f"_(Action batch rejected: `{err}`. No actions taken.)_")
            log(f"MENTION action validation FAILED: agent={agent_id} error={err}")
        else:
            results, pr_previews = await _execute_action_batch(
                valid, config, agent_id=agent_id, channel_id=int(message.channel.id),
            )
            summary_lines = [f"**Actions taken by `{agent_id}`:**"]
            for line in results:
                summary_lines.append(f"- {line}")
            await message.channel.send("\n".join(summary_lines)[:1900])
            # PR proposals get their preview as a separate message (richer
            # formatting, won't get clipped by the action-summary truncation).
            for preview in pr_previews:
                await message.channel.send(preview[:1900])
            log(f"MENTION actions executed: agent={agent_id} count={len(valid)}")

    log(f"MENTION done: agent={agent_id} chars={len(response)} actions={len(parsed.actions or [])}")


async def _execute_action_batch(
    actions: list[dict],
    config: BotConfig,
    *,
    agent_id: str,
    channel_id: int,
) -> tuple[list[str], list[str]]:
    """Execute each validated action via gh wrappers.

    Returns (result_lines, pr_preview_blocks). Most actions execute inline
    and contribute a result line; `open_pr_with_changes` is the exception
    — it stages a proposal and contributes both a short result line AND
    a longer preview block that the caller posts as a separate message.
    """
    results: list[str] = []
    pr_previews: list[str] = []
    repo = config.default_repo
    for action in actions:
        atype = action["action"]
        try:
            if atype == "create_issue":
                r = await create_issue(
                    repo=repo,
                    title=action["title"],
                    body=action["body"],
                    labels=action.get("labels"),
                )
                if r.ok:
                    results.append(f"✅ Created #{r.issue_number}: `{action['title'][:60]}` → {r.issue_url}")
                else:
                    results.append(f"❌ create_issue (`{action['title'][:40]}…`): `{r.error}`")
            elif atype == "comment":
                r = await comment_issue(repo=repo, issue_number=action["issue_number"], body=action["body"])
                if r.ok:
                    results.append(f"✅ Commented on #{action['issue_number']}")
                else:
                    results.append(f"❌ comment on #{action['issue_number']}: `{r.error}`")
            elif atype == "add_label":
                fails = []
                for lbl in action["labels"]:
                    r = await add_label_async(repo=repo, issue_number=action["issue_number"], label=lbl)
                    if not r.ok:
                        fails.append((lbl, r.error))
                if not fails:
                    results.append(f"✅ Added labels {action['labels']} to #{action['issue_number']}")
                else:
                    results.append(f"❌ add_label on #{action['issue_number']}: " + ", ".join(f"{l}({e})" for l, e in fails))
            elif atype == "remove_label":
                fails = []
                for lbl in action["labels"]:
                    r = await remove_label_async(repo=repo, issue_number=action["issue_number"], label=lbl)
                    if not r.ok:
                        fails.append((lbl, r.error))
                if not fails:
                    results.append(f"✅ Removed labels {action['labels']} from #{action['issue_number']}")
                else:
                    results.append(f"❌ remove_label on #{action['issue_number']}: " + ", ".join(f"{l}({e})" for l, e in fails))
            elif atype == "close_issue":
                r = await close_issue(
                    repo=repo,
                    issue_number=action["issue_number"],
                    comment=action.get("comment"),
                )
                if r.ok:
                    results.append(f"✅ Closed #{action['issue_number']}")
                else:
                    results.append(f"❌ close_issue #{action['issue_number']}: `{r.error}`")
            elif atype == "open_pr_with_changes":
                # Two-phase: validate + stage; don't execute yet. The push
                # happens only after Andy replies `approve <id>` in this
                # channel (intercepted by handle_pr_approval below).
                validated = validate_open_pr_action(
                    action, agent_id=agent_id, channel_id=channel_id,
                )
                if not validated.ok:
                    results.append(f"❌ open_pr_with_changes: `{validated.error}`")
                else:
                    PR_STORE.stage(validated.proposal)
                    pr_previews.append(format_pr_preview(validated.proposal))
                    results.append(
                        f"🛫 Staged PR proposal `{validated.proposal.proposal_id}` for "
                        f"`{validated.proposal.repo.gh_slug}` "
                        f"(awaiting `approve {validated.proposal.proposal_id}` in this channel)"
                    )
            elif atype == "create_agent":
                # Run the full agent_builder workflow in a thread (it does
                # synchronous git/gh subprocess calls).
                import asyncio as _asyncio
                loop = _asyncio.get_running_loop()
                r = await loop.run_in_executor(
                    None,
                    lambda: execute_create_agent(action, repo),
                )
                if r.ok:
                    line = (
                        f"✅ Created agent `{r.agent_id}`: branch `{r.branch}`, PR {r.pr_url}"
                    )
                    if r.skipped_reasons:
                        line += f" _(skipped: {'; '.join(r.skipped_reasons)})_"
                    results.append(line)
                else:
                    results.append(f"❌ create_agent `{action.get('agent_id')}`: `{r.error}`")
            else:
                # Should be unreachable due to validate_action_batch().
                results.append(f"❌ unknown action type: `{atype}`")
        except Exception as exc:
            results.append(f"❌ {atype}: exception `{type(exc).__name__}: {exc}`")
    return results, pr_previews


# ---------- handle_pr_approval: Andy approves/cancels a staged PR proposal ----------

async def handle_pr_approval(client, message, config: BotConfig) -> bool:
    """Intercept approve/cancel text in a channel that has a pending
    PR proposal for THIS bot's agent. Returns True if the message was
    handled (caller should `return` and skip mention routing), False
    otherwise (no pending proposal, or text doesn't match).

    Only authorized users (Andy) can approve/cancel — the auth check is
    upstream in on_message before this is called.
    """
    text = (message.content or "").strip()
    agent_id = client.agent.id
    channel_id = int(message.channel.id)

    approval_ok, approval_id = is_approval_text(text)
    cancel_ok, cancel_id = is_cancel_text(text)
    if not approval_ok and not cancel_ok:
        return False

    # Specific id → look up directly. Generic approve/cancel → pick the
    # most recent pending proposal in this channel by this agent.
    target_id = approval_id or cancel_id
    if target_id:
        proposal = PR_STORE.get(target_id)
        if proposal is None or proposal.channel_id != channel_id or proposal.agent_id != agent_id:
            # Don't claim a proposal that belongs to another agent or channel.
            return False
    else:
        proposal = PR_STORE.peek_for_channel_agent(channel_id, agent_id)
        if proposal is None:
            return False

    if cancel_ok:
        PR_STORE.pop(proposal.proposal_id)
        await message.channel.send(
            f"_Cancelled PR proposal `{proposal.proposal_id}` for `{proposal.repo.gh_slug}`. "
            f"No branch was created. Nothing pushed._"
        )
        log(f"PR_APPROVAL cancel: agent={agent_id} id={proposal.proposal_id}")
        return True

    # Approval path. Pop first (so a second `approve` doesn't double-fire
    # while we're pushing), then execute in a thread (synchronous git/gh).
    PR_STORE.pop(proposal.proposal_id)
    await message.channel.send(
        f"_Approved `{proposal.proposal_id}` — opening PR against `{proposal.repo.gh_slug}` "
        f"on branch `{proposal.branch}`. This takes ~10–20 seconds._"
    )
    import asyncio as _asyncio
    loop = _asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, lambda: execute_proposal(proposal))
    except Exception as exc:
        await message.channel.send(
            f"❌ PR `{proposal.proposal_id}` crashed during execution: "
            f"`{type(exc).__name__}: {exc}`"
        )
        log(f"PR_APPROVAL execute CRASH: agent={agent_id} id={proposal.proposal_id} {type(exc).__name__}: {exc}")
        return True

    if result.ok:
        await message.channel.send(
            f"✅ PR opened: {result.pr_url}\n"
            f"_(branch: `{result.branch}` on `{proposal.repo.gh_slug}`. "
            f"Review, edit, merge from your end.)_"
        )
        log(f"PR_APPROVAL ok: agent={agent_id} id={proposal.proposal_id} url={result.pr_url}")
    else:
        await message.channel.send(
            f"❌ PR `{proposal.proposal_id}` failed: `{result.error}`"
            + (f"\n_(branch `{result.branch}` may have been pushed; check manually.)_" if result.branch else "")
        )
        log(f"PR_APPROVAL fail: agent={agent_id} id={proposal.proposal_id} err={result.error}")
    return True


# ---------- on_message: thread replies that continue an intake session ----------

async def on_thread_message(message: discord.Message, config: BotConfig) -> None:
    """Called from senior-pm's on_message. Returns silently for non-intake messages."""
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.Thread):
        return

    thread_id = str(message.channel.id)
    if not INTAKE.has(thread_id):
        return  # not a PM intake thread

    user_id = str(message.author.id)
    if not is_authorized(user_id, config):
        log(f"REFUSED thread reply: user_id={user_id} thread={thread_id}")
        await message.channel.send(REFUSAL_MESSAGE)
        return

    session, response = INTAKE.continue_session(thread_id=thread_id, user_message=message.content)
    log(f"INTAKE turn: thread={thread_id} state={session.state}")
    await message.channel.send(response)

    if session.state == SessionState.READY_TO_FILE and (response.startswith("Filing") or "Filing the issue" in response):
        await _file_issue(message.channel, config, thread_id)


async def _file_issue(thread: discord.Thread, config: BotConfig, thread_id: str) -> None:
    thread_url = f"https://discord.com/channels/{config.guild_id}/{thread.parent_id}/{thread.id}"
    title, body = INTAKE.render_issue_body(thread_id, thread_url=thread_url)

    log(f"INTAKE filing issue: thread={thread_id} title={title!r}")
    result = await create_issue(
        repo=config.default_repo,
        title=title,
        body=body,
        labels=["pm-managed", "needs-input"],
    )

    if not result.ok:
        log(f"INTAKE file FAILED: thread={thread_id} error={result.error}")
        await thread.send(
            f"**Failed to file issue.** `{result.error}`\n\n"
            f"Reply `go` to retry, or `cancel` to drop this task."
        )
        # Re-arm the session so 'go' triggers another file attempt
        session = INTAKE.get(thread_id)
        if session is not None:
            session.state = SessionState.READY_TO_FILE
        return

    INTAKE.mark_filed(thread_id=thread_id, issue_number=result.issue_number)
    THREAD_MAP.bind(result.issue_number, thread_id)
    log(f"INTAKE filed: thread={thread_id} issue=#{result.issue_number}")

    # Mirror to Obsidian vault. Failing the mirror does NOT fail the file:
    # GitHub is the canonical record; vault is a mirror.
    session = INTAKE.get(thread_id)
    if session is not None:
        try:
            note_path = VAULT.write_initial_note(
                issue_number=result.issue_number,
                title=title,
                issue_url=result.issue_url,
                source_request=session.original_request,
                closure_criteria=session.closure_criteria(),
                initial_owner="senior-pm",
                discord_thread_url=thread_url,
            )
            log(f"VAULT mirror: issue=#{result.issue_number} -> {note_path}")
        except Exception as exc:
            log(f"VAULT mirror FAILED (non-fatal): issue=#{result.issue_number} {type(exc).__name__}: {exc}")

    await thread.send(
        f"**Filed as #{result.issue_number}.** {result.issue_url}\n\n"
        f"This thread stays open for follow-ups. Senior-pm or a specialist will pick it up next."
    )


# ---------- other slash commands: still stubs (PR-K) ----------

async def handle_pm_summary(interaction: discord.Interaction, config: BotConfig) -> None:
    if not await _gate(interaction, config, "pm-summary"):
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    if not SUMMARY_PROMPT_PATH.exists():
        await interaction.followup.send(
            f"Internal error: pm_summary prompt missing at {SUMMARY_PROMPT_PATH}", ephemeral=True
        )
        return

    log(f"PM_SUMMARY start: repo={config.default_repo}")

    ok, issues, err = await list_open_issues(config.default_repo)
    if not ok:
        await interaction.followup.send(f"Failed to list open issues: `{err}`", ephemeral=True)
        log(f"PM_SUMMARY fetch FAILED: error={err}")
        return

    if not issues:
        await interaction.followup.send(
            f"**Open: 0** · No open issues on `{config.default_repo}`. Board is clean.",
            ephemeral=True,
        )
        log("PM_SUMMARY: no open issues")
        return

    template = SUMMARY_PROMPT_PATH.read_text(encoding="utf-8")
    prompt = build_summary_prompt(template, repo=config.default_repo, issues=issues)

    ok, stdout, err = await call_claude(prompt)
    if not ok:
        await interaction.followup.send(f"claude -p failed for pm-summary: `{err}`", ephemeral=True)
        log(f"PM_SUMMARY claude FAILED: error={err}")
        return

    summary_text = truncate_for_discord(stdout.strip())
    await interaction.followup.send(summary_text, ephemeral=True)
    log(f"PM_SUMMARY done: open={len(issues)} chars={len(summary_text)}")


async def handle_squad_discuss(interaction: discord.Interaction, config: BotConfig, topic: str) -> None:
    if not await _gate(interaction, config, "squad-discuss"):
        return

    topic = topic.strip()
    if not topic:
        await interaction.response.send_message("Empty topic. Give me something to discuss.", ephemeral=True)
        return

    channel = interaction.channel
    if channel is None or not hasattr(channel, "create_thread"):
        await interaction.response.send_message(
            "Run /squad-discuss from a text channel that supports threads (e.g., #neural-bridge).",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    if not FRAMING_PROMPT_PATH.exists() or not TURN_PROMPT_PATH.exists():
        await interaction.followup.send("Internal error: squad-discuss prompts missing.", ephemeral=True)
        return

    log(f"SQUAD_DISCUSS start: topic={topic[:80]!r}")

    # 1. Senior-pm drafts framing + picks specialists.
    framing_template = FRAMING_PROMPT_PATH.read_text(encoding="utf-8")
    framing_prompt = build_framing_prompt(framing_template, topic=topic)
    ok, stdout, err = await call_claude(framing_prompt)
    if not ok:
        await interaction.followup.send(f"Framing failed: `{err}`", ephemeral=True)
        log(f"SQUAD_DISCUSS framing FAILED: error={err}")
        return

    text = strip_code_fences(stdout)
    try:
        framing_data = json.loads(text)
    except json.JSONDecodeError as exc:
        await interaction.followup.send(
            f"Framing output was not valid JSON: `{exc.msg}`. Raw output in stderr log.",
            ephemeral=True,
        )
        log(f"SQUAD_DISCUSS framing parse FAILED: raw={text[:200]!r}")
        return

    ok, schema_err = validate_framing_output(framing_data)
    if not ok:
        await interaction.followup.send(f"Framing schema check failed: `{schema_err}`", ephemeral=True)
        log(f"SQUAD_DISCUSS framing schema FAILED: {schema_err}")
        return

    framing = truncate_framing(framing_data["framing"])
    selected = framing_data["selected_agents"]
    log(f"SQUAD_DISCUSS picked: {selected}")

    # 2. Create the thread.
    truncated_name = topic if len(topic) <= 80 else topic[:79] + "…"
    try:
        thread = await channel.create_thread(  # type: ignore[union-attr]
            name=f"squad: {truncated_name}",
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to create threads in this channel.",
            ephemeral=True,
        )
        return

    # 3. Senior-pm posts framing as itself (this client).
    await thread.send(f"**Squad discussion** · topic: _{topic[:200]}_\n\n{framing}")

    # 4. Each selected specialist posts a turn AS itself via the registry.
    turn_template = TURN_PROMPT_PATH.read_text(encoding="utf-8")
    turn_outcomes: list[str] = []
    for agent_id in selected:
        turn_prompt = build_turn_prompt(turn_template, agent_id=agent_id, topic=topic, framing=framing)
        ok, stdout, err = await call_claude(turn_prompt)
        if not ok:
            log(f"SQUAD_DISCUSS turn FAILED ({agent_id}): {err}")
            turn_outcomes.append(f"{agent_id}: failed ({err})")
            continue
        turn_text = truncate_turn(stdout)
        ok_post, err_post = await post_as_agent(agent_id, thread_id=thread.id, content=turn_text)
        if ok_post:
            turn_outcomes.append(f"{agent_id}: posted")
            log(f"SQUAD_DISCUSS turn posted ({agent_id})")
        else:
            log(f"SQUAD_DISCUSS turn post FAILED ({agent_id}): {err_post}")
            # Fall back to senior-pm posting it on their behalf
            await thread.send(f"_(could not post as `{agent_id}` ({err_post}); turn below)_\n\n{turn_text}")
            turn_outcomes.append(f"{agent_id}: fallback")

    # 5. Senior-pm closes the round.
    await thread.send(
        "_Round 1 complete. Reply in this thread to continue, or use `/triage <issue#>` "
        "or `/pm-task` to convert this discussion into action._"
    )

    await interaction.followup.send(
        f"Discussion opened in <#{thread.id}> with `{', '.join(selected)}`. Outcomes: "
        + "; ".join(turn_outcomes),
        ephemeral=True,
    )
    log(f"SQUAD_DISCUSS done: thread={thread.id} outcomes={turn_outcomes}")


async def handle_triage(interaction: discord.Interaction, config: BotConfig, issue_number: int) -> None:
    if not await _gate(interaction, config, "triage"):
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    if not TRIAGE_PROMPT_PATH.exists():
        await interaction.followup.send(
            f"Internal error: triage prompt missing at {TRIAGE_PROMPT_PATH}", ephemeral=True
        )
        return

    log(f"TRIAGE start: issue=#{issue_number} repo={config.default_repo}")

    ok, issue, err = await fetch_issue(config.default_repo, issue_number)
    if not ok:
        await interaction.followup.send(f"Failed to fetch issue #{issue_number}: `{err}`", ephemeral=True)
        log(f"TRIAGE fetch FAILED: issue=#{issue_number} error={err}")
        return

    template = TRIAGE_PROMPT_PATH.read_text(encoding="utf-8")
    prompt = build_triage_prompt(template, repo=config.default_repo, issue_number=issue_number, issue=issue)

    ok, stdout, err = await call_claude(prompt)
    if not ok:
        await interaction.followup.send(f"claude -p failed for triage: `{err}`", ephemeral=True)
        log(f"TRIAGE claude FAILED: issue=#{issue_number} error={err}")
        return

    text = strip_code_fences(stdout)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        await interaction.followup.send(
            f"Triage output was not valid JSON: `{exc.msg}`. Raw output preserved in stderr log.",
            ephemeral=True,
        )
        log(f"TRIAGE parse FAILED: issue=#{issue_number} raw={text[:200]!r}")
        return

    ok, schema_err = validate_triage_output(data)
    if not ok:
        await interaction.followup.send(
            f"Triage output failed schema check: `{schema_err}`",
            ephemeral=True,
        )
        log(f"TRIAGE schema FAILED: issue=#{issue_number} error={schema_err}")
        return

    # Auto-fixes: apply any high-confidence patches to the issue body BEFORE
    # the quality-flag gate, so the gate only fires for things actually
    # needing Andy's input.
    auto_fixes = data.get("auto_fixes", [])
    auto_fix_applied: list[str] = []
    if auto_fixes:
        original_body = issue.get("body") or ""
        new_body, auto_fix_applied = apply_auto_fixes(original_body, auto_fixes)
        if auto_fix_applied:
            edit_result = await edit_issue_body(
                repo=config.default_repo,
                issue_number=issue_number,
                new_body=new_body,
            )
            if edit_result.ok:
                log(f"TRIAGE auto-fixes applied: issue=#{issue_number} count={len(auto_fix_applied)}")
            else:
                log(f"TRIAGE auto-fix edit FAILED (non-fatal): issue=#{issue_number} error={edit_result.error}")
                # Treat as not-applied so we don't claim success in the comment.
                auto_fix_applied = []
        else:
            log(f"TRIAGE auto-fixes idempotent: issue=#{issue_number} (sections already present)")

    # Quality-flag gate: if the triage surfaced any quality flags, the issue
    # has gaps that block a specialist from starting. Override the state to
    # needs-human regardless of what the model recommended. Andy unblocks by
    # editing the issue body to address the flags, then re-running /triage.
    effective_state = data["recommended_state"]
    if data["quality_flags"]:
        if effective_state != "needs-human":
            log(f"TRIAGE override: quality_flags non-empty -> state {effective_state} -> needs-human")
        effective_state = "needs-human"

    # Apply label changes. Always include the effective_state in adds and
    # remove any stale state labels.
    current_state_labels = [
        lbl["name"]
        for lbl in issue.get("labels", [])
        if isinstance(lbl, dict) and lbl.get("name") in STATE_LABEL_SET
    ]
    add_set = set(data["labels_to_add"]) | {effective_state}
    remove_set = set(data["labels_to_remove"]) | (set(current_state_labels) - {effective_state})
    # Don't try to remove what we're adding.
    remove_set -= add_set

    applied, failures = await apply_labels(
        repo=config.default_repo,
        issue_number=issue_number,
        add=sorted(add_set),
        remove=sorted(remove_set),
    )
    log(f"TRIAGE labels applied: issue=#{issue_number} applied={applied} failures={failures}")

    # Post a triage report comment on the issue. Quality flags render as a
    # task list so Andy can check items off as he edits the issue body.
    auto_fixes_md = ""
    if auto_fix_applied:
        auto_fixes_md = (
            "\n\n**Auto-fixes applied to issue body:**\n"
            + "\n".join(f"- ✅ {desc}" for desc in auto_fix_applied)
            + "\n\n_If any of these are wrong, edit the issue body to correct them._"
        )

    quality_flags_md = ""
    if data["quality_flags"]:
        quality_flags_md = (
            "\n\n**Address these to unblock the specialist** "
            "(state auto-set to `needs-human` until done):\n"
            + "\n".join(f"- [ ] {f}" for f in data["quality_flags"])
            + "\n\nRe-run `/triage` after editing the issue body to confirm gaps are resolved."
        )
    failures_md = ""
    if failures:
        failures_md = "\n\n_Label changes that failed: " + ", ".join(f"`{lbl}` ({reason})" for lbl, reason in failures) + "_"

    comment_body = (
        f"**senior-pm triage**\n\n"
        f"- Recommended specialist: `{data['recommended_specialist']}`\n"
        f"- Priority: `{data['priority']}`\n"
        f"- Effective state: `{effective_state}`"
        + (f" _(model recommended `{data['recommended_state']}`; downgraded due to quality flags)_"
           if effective_state != data['recommended_state'] else "")
        + f"\n- Labels applied: `{', '.join(applied) if applied else 'none'}`\n\n"
        f"**Reason.** {data['reason']}"
        f"{auto_fixes_md}"
        f"{quality_flags_md}"
        f"{failures_md}"
    )

    # Use gh issue comment to post
    import subprocess as sp
    try:
        sp.run(
            ["gh", "issue", "comment", str(issue_number), "--repo", config.default_repo, "--body", comment_body],
            capture_output=True, text=True, timeout=30, stdin=sp.DEVNULL, check=False,
        )
    except Exception as exc:
        log(f"TRIAGE comment FAILED (non-fatal): {type(exc).__name__}: {exc}")

    # Mirror to vault
    try:
        path = VAULT.append_status(
            issue_number=issue_number,
            line=f"triaged → {data['recommended_specialist']} ({data['priority']}, {data['recommended_state']})",
        )
        if path is not None:
            log(f"VAULT triage-mirror: issue=#{issue_number} -> {path}")
    except Exception as exc:
        log(f"VAULT triage-mirror FAILED (non-fatal): {type(exc).__name__}: {exc}")

    # Hand off in Discord — if there's a bound thread for this issue and the
    # recommended specialist is a different bot, post AS that specialist in
    # the thread. Visual signal: each agent speaks as itself.
    handoff_status = ""
    bound_thread = THREAD_MAP.get_thread(issue_number)
    specialist = data["recommended_specialist"]
    if bound_thread is not None and specialist != "senior-pm":
        handoff_msg = (
            f"**Hand-off from senior-pm.** I'm picking up #{issue_number}.\n"
            f"- Priority: `{data['priority']}`\n"
            f"- Reason: {data['reason']}\n\n"
            f"_I'll comment on the issue with progress; this thread stays open for follow-ups._"
        )
        ok_post, err_post = await post_as_agent(specialist, thread_id=int(bound_thread), content=handoff_msg)
        if ok_post:
            log(f"HANDOFF: issue=#{issue_number} specialist={specialist} posted in thread={bound_thread}")
            handoff_status = f"\n\n**{specialist}** has been notified in the bound thread."
        else:
            log(f"HANDOFF FAILED (non-fatal): issue=#{issue_number} specialist={specialist} error={err_post}")
            handoff_status = f"\n\n_(Hand-off post failed: `{err_post}`. {specialist} bot may not be online.)_"

    # Reply to Andy
    summary = (
        f"**Triaged issue #{issue_number}**\n"
        f"- Specialist: `{data['recommended_specialist']}`\n"
        f"- Priority: `{data['priority']}` · State: `{effective_state}`"
    )
    if effective_state != data["recommended_state"]:
        summary += f" _(downgraded from `{data['recommended_state']}` because of quality flags)_"
    summary += (
        f"\n- Labels: `{', '.join(applied) if applied else 'none'}`\n"
        f"- Reason: {data['reason']}"
        f"{handoff_status}"
    )
    if failures:
        summary += f"\n\n_(Some label changes failed; see issue comment for details.)_"
    if auto_fix_applied:
        fixes_list = "\n".join(f"  - ✅ {desc}" for desc in auto_fix_applied)
        summary += f"\n\n**Auto-fixes applied to the issue body:**\n{fixes_list}"
    if data["quality_flags"]:
        flag_list = "\n".join(f"  - [ ] {f}" for f in data["quality_flags"])
        summary += f"\n\n**Address these to unblock:**\n{flag_list}\n\nRe-run `/triage {issue_number}` after editing the issue body."
    await interaction.followup.send(summary, ephemeral=True)
    log(f"TRIAGE done: issue=#{issue_number} specialist={data['recommended_specialist']} state={effective_state} auto_fixes={len(auto_fix_applied)}")


async def handle_close(interaction: discord.Interaction, config: BotConfig, issue_number: int) -> None:
    if not await _gate(interaction, config, "close"):
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    closing_comment = (
        "Closed via Discord bot per Andy's authorization. "
        "Closure criteria checked against the issue body."
    )
    result = await close_issue(
        repo=config.default_repo,
        issue_number=issue_number,
        comment=closing_comment,
    )

    if not result.ok:
        log(f"CLOSE FAILED: issue=#{issue_number} error={result.error}")
        await interaction.followup.send(
            f"Failed to close issue #{issue_number}: `{result.error}`", ephemeral=True
        )
        return

    log(f"CLOSED: issue=#{issue_number}")

    # Mirror to vault (non-fatal if missing).
    try:
        path = VAULT.append_status(
            issue_number=issue_number,
            line="closed via Discord bot",
            new_status="closed",
        )
        if path is not None:
            log(f"VAULT close-mirror: issue=#{issue_number} -> {path}")
    except Exception as exc:
        log(f"VAULT close-mirror FAILED (non-fatal): issue=#{issue_number} {type(exc).__name__}: {exc}")

    # If the issue had a bound thread, post a notice there.
    thread_id = THREAD_MAP.get_thread(issue_number)
    if thread_id is not None:
        # Best-effort: try to fetch the thread and post. Failure is non-fatal.
        try:
            thread = interaction.client.get_channel(int(thread_id))
            if thread is not None and isinstance(thread, discord.Thread):
                await thread.send(f"**Issue #{issue_number} closed.**")
        except Exception as exc:
            log(f"thread close-notice FAILED (non-fatal): {type(exc).__name__}: {exc}")

    await interaction.followup.send(
        f"Closed issue #{issue_number}. Vault note updated if it existed.",
        ephemeral=True,
    )
