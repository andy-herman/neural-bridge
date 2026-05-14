"""Pure helpers for the `handoff_to_squad` action.

Luna emits `handoff_to_squad` from a DM with Andy when the DM surfaces
work that needs other specialists. The daemon posts a summary into the
configured squad channel (e.g., #neural-bridge), @-mentioning the named
agents from their bot client_ids so Discord actually fires their
on_message handlers.

This module is discord-free so the test suite runs on system Python.
The handlers.py dispatch wires these helpers into the live message
flow.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_POST_CHARS = 1900  # Discord hard cap is 2000; leave headroom.


@dataclass
class ResolveResult:
    ok: bool
    client_ids: list[str]
    unknown: list[str]
    error: str | None = None


def resolve_mentions(mentions: list[str], agents_by_id: dict[str, str]) -> ResolveResult:
    """Map a list of agent_ids to their Discord bot client_ids.

    `agents_by_id` is `{agent_id: client_id}` (string-typed snowflakes).
    Returns ok=True only if every mention resolves. Unknown mentions are
    listed in `unknown` for an actionable error message.
    """
    resolved: list[str] = []
    unknown: list[str] = []
    for m in mentions:
        cid = agents_by_id.get(m)
        if cid is None:
            unknown.append(m)
        else:
            resolved.append(cid)
    if unknown:
        return ResolveResult(
            ok=False, client_ids=[], unknown=unknown,
            error=f"unknown agent(s): {', '.join(unknown)}",
        )
    return ResolveResult(ok=True, client_ids=resolved, unknown=[])


def build_handoff_post(
    *,
    summary: str,
    mention_client_ids: list[str],
    dm_excerpt: str | None = None,
) -> str:
    """Compose the squad-channel message body.

    Format:

        📨 Handoff from Luna (DM with Andy):

        <@CLIENT_ID_1> <@CLIENT_ID_2>

        {summary}

        > "{dm_excerpt}"   (only if excerpt provided)

    Truncates to MAX_POST_CHARS with an ellipsis if needed; the summary
    is the part that gets cut, not the mentions or the excerpt.
    """
    mention_line = " ".join(f"<@{cid}>" for cid in mention_client_ids)
    parts = [
        "📨 Handoff from Luna (DM with Andy):",
        "",
        mention_line,
        "",
        summary.strip(),
    ]
    if dm_excerpt and dm_excerpt.strip():
        parts.extend(["", f"> {dm_excerpt.strip()}"])

    body = "\n".join(parts)
    if len(body) <= MAX_POST_CHARS:
        return body

    # Over budget. Trim the summary while keeping the structural parts intact.
    overhead = len(body) - len(summary.strip())
    keep = MAX_POST_CHARS - overhead - 1  # 1 for the ellipsis
    if keep < 100:
        # Pathological: even the structural overhead is too big. Just hard-cap.
        return body[: MAX_POST_CHARS - 1] + "…"
    trimmed_summary = summary.strip()[:keep].rstrip() + "…"
    parts[4] = trimmed_summary
    return "\n".join(parts)


def build_dm_confirmation(
    *,
    mentions: list[str],
    squad_channel_id: int,
    message_id: int | None = None,
    guild_id: int | None = None,
) -> str:
    """Compose the DM reply that confirms the handoff posted successfully.

    If message_id and guild_id are both provided, includes a deep link.
    """
    agent_list = ", ".join(f"`@{m}`" for m in mentions)
    line = f"✅ Posted to <#{squad_channel_id}>, pulling in {agent_list}."
    if message_id is not None and guild_id is not None:
        link = f"https://discord.com/channels/{guild_id}/{squad_channel_id}/{message_id}"
        line += f" Link: {link}"
    return line
