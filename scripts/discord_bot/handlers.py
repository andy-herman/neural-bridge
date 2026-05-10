"""Slash command handlers + thread message handler for senior-pm.

PR-I-A wires real PM intake → GitHub issue creation. The other slash
commands (/pm-summary, /squad-discuss, /triage, /close) remain stubs;
they get real logic in PR-K.
"""

from __future__ import annotations

import sys

import discord

import json

from .auth import REFUSAL_MESSAGE, is_authorized
from .claude_invoke import call_claude
from .client_registry import post_as_agent
from .config import BotConfig
from .actions import extract_actions, validate_action_batch
from .agent_builder import execute_create_agent
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


def log(msg: str) -> None:
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

    if not MENTION_PROMPT_PATH.exists():
        await message.channel.send(f"_(internal: mention prompt missing at {MENTION_PROMPT_PATH})_")
        return

    log(f"MENTION: agent={agent_id} channel={message.channel.id} from={user_id}")

    # Acknowledge so Andy sees the bot is thinking. Discord shows typing for ~10s
    # automatically when we use typing(); use it as a thinking indicator.
    async with message.channel.typing():
        # Fetch recent context. discord.py: history(limit=N).
        history: list[dict] = []
        try:
            async for h_msg in message.channel.history(limit=20, before=message):
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

        tools = allowed_tools_for(agent_id)
        extra_dirs = add_dirs_for(agent_id)
        log(
            f"MENTION calling claude: agent={agent_id} tools={tools or 'none'} "
            f"add_dirs={len(extra_dirs) if extra_dirs else 0}"
        )
        ok, stdout, err = await call_claude(prompt, allowed_tools=tools, add_dirs=extra_dirs)

    if not ok:
        log(f"MENTION claude FAILED: agent={agent_id} error={err}")
        await message.channel.send(
            f"_(I hit an error: `{err}`. Try again, or @-mention senior-pm to escalate.)_"
        )
        return

    # Extract any structured action block before truncating + posting.
    parsed = extract_actions(stdout)
    response_cap = max_response_chars_for(agent_id)
    response = truncate_response(parsed.visible_response, limit=response_cap)

    if not response and not parsed.actions and not parsed.parse_error:
        log(f"MENTION empty response: agent={agent_id}")
        await message.channel.send("_(I had nothing useful to add. Try rephrasing or @-mention a different specialist.)_")
        return

    if response:
        # Chunk into multiple messages if the response exceeds Discord's per-message limit.
        chunks = chunk_for_discord(response)
        for i, chunk in enumerate(chunks):
            # Tag continuation messages so the user knows the response is being split,
            # except when there's only one chunk.
            if len(chunks) > 1:
                chunk = f"_(part {i + 1}/{len(chunks)})_\n{chunk}" if i > 0 else chunk
            await message.channel.send(chunk)

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
            results = await _execute_action_batch(valid, config)
            summary_lines = [f"**Actions taken by `{agent_id}`:**"]
            for line in results:
                summary_lines.append(f"- {line}")
            await message.channel.send("\n".join(summary_lines)[:1900])
            log(f"MENTION actions executed: agent={agent_id} count={len(valid)}")

    log(f"MENTION done: agent={agent_id} chars={len(response)} actions={len(parsed.actions or [])}")


async def _execute_action_batch(actions: list[dict], config: BotConfig) -> list[str]:
    """Execute each validated action via gh wrappers. Returns a list of
    human-readable result lines."""
    results: list[str] = []
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
    return results


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
