"""Slash command handlers + thread message handler for senior-pm.

PR-I-A wires real PM intake → GitHub issue creation. The other slash
commands (/pm-summary, /squad-discuss, /triage, /close) remain stubs;
they get real logic in PR-K.
"""

from __future__ import annotations

import sys

import discord

from .auth import REFUSAL_MESSAGE, is_authorized
from .config import BotConfig
from .github_client import close_issue, create_issue
from .obsidian_writer import ObsidianWriter
from .pm_intake import PMIntake, SessionState
from .thread_map import ThreadMap


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
    await interaction.response.send_message(
        "_Executive summary ships in PR-K (hand-offs + state machine pass)._",
        ephemeral=True,
    )


async def handle_squad_discuss(interaction: discord.Interaction, config: BotConfig, topic: str) -> None:
    if not await _gate(interaction, config, "squad-discuss"):
        return
    await interaction.response.send_message(
        f"Received squad-discuss topic: `{topic[:200]}`\n\n_Multi-agent huddle ships in a later PR._",
        ephemeral=True,
    )


async def handle_triage(interaction: discord.Interaction, config: BotConfig, issue_number: int) -> None:
    if not await _gate(interaction, config, "triage"):
        return
    await interaction.response.send_message(
        f"Received triage request for issue #{issue_number}. _senior-pm triage ships in PR-K._",
        ephemeral=True,
    )


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
