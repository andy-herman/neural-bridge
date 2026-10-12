"""Action items: an agent's gated Discord action, waiting on Andy.

The only gated action today is `open_pr_with_changes`. The Discord daemon
validates it (scripts/discord_bot/pr_proposals.py), then stages it here instead
of in the in-memory store it used until 2026-10, where a pending approval lived
fifteen minutes and died with any restart.

Two ways to decide, one store:
  - Telegram: the card's Approve (two taps) or Reject; Luna's bridge applies.
  - Discord: `approve <id>` / `cancel <id>` in the channel, as before; the
    daemon decides, claims and applies in its own process.
Whichever lands second is told the item was already handled.

A proposal carries whole file contents written against main as it was when
the agent wrote them, so it expires after ACTION_TTL_SECONDS rather than
pushing yesterday's view of a file over today's.
"""

from __future__ import annotations

from . import store as st

SOURCE = "discord"
ACTION_TTL_SECONDS = 24 * 3600


def stage(proposal, *, store: st.Store | None = None, now: int | None = None) -> st.Item:
    """Queue a validated PRProposal as an action item."""
    store = store or st.Store()
    files = [[path, content] for path, content in proposal.files]
    item, _ = store.raise_item(
        source=SOURCE, key=proposal.proposal_id, kind=st.ACTION,
        title=f"{proposal.agent_id} wants to open a PR on {proposal.repo.repo_id}",
        detail=(f"{proposal.pr_title} · {len(files)} file{'s' if len(files) != 1 else ''} "
                f"on branch {proposal.branch}"),
        agent=proposal.agent_id,
        payload={"action": "open_pr_with_changes", "repo": proposal.repo.repo_id,
                 "channel_id": int(proposal.channel_id), "branch": proposal.branch,
                 "files": files, "commit_message": proposal.commit_message,
                 "pr_title": proposal.pr_title, "pr_body": proposal.pr_body},
        unique=True, now=now)
    return item


def proposal_from(item: st.Item):
    """The PRProposal an action item holds, or raises ValueError."""
    from scripts.discord_bot.pr_proposals import PRProposal
    from scripts.discord_bot.repos import repo_for

    p = item.payload
    if p.get("action") != "open_pr_with_changes":
        raise ValueError(f"unknown action {p.get('action')!r}")
    repo = repo_for(str(p.get("repo") or ""))
    if repo is None:
        raise ValueError(f"unknown repo {p.get('repo')!r}")
    return PRProposal(
        proposal_id=item.id, agent_id=item.agent, channel_id=int(p.get("channel_id") or 0),
        repo=repo, branch=str(p["branch"]),
        files=[(str(path), str(content)) for path, content in p.get("files") or []],
        commit_message=str(p["commit_message"]), pr_title=str(p["pr_title"]),
        pr_body=str(p.get("pr_body") or ""), created_at=float(item.created_at))


def apply(item: st.Item, *, execute=None) -> tuple[bool, str]:
    """Applier: approve pushes the branch and opens the PR; reject drops it."""
    if item.verb == st.REJECT:
        return True, "dropped; nothing was pushed"
    if item.verb != st.APPROVE:
        return False, f"actions take approve or reject, not {item.verb!r}"
    try:
        proposal = proposal_from(item)
    except (KeyError, ValueError) as exc:
        return False, f"unreadable proposal: {exc}"
    if execute is None:
        from scripts.discord_bot.pr_proposals import execute_proposal as execute
    result = execute(proposal)
    if result.ok:
        return True, result.pr_url or f"PR opened from branch {result.branch}"
    note = f" (branch {result.branch} may have been pushed)" if result.branch else ""
    return False, f"{result.error}{note}"


def is_stale(item: st.Item, now: int) -> bool:
    return item.kind == st.ACTION and now - item.created_at >= ACTION_TTL_SECONDS


def expire_stale(store: st.Store, now: int) -> list[str]:
    """Supersede every undecided action older than the TTL."""
    return [i.id for i in store.items(states=(st.OPEN, st.SNOOZED), kinds=(st.ACTION,))
            if is_stale(i, now) and store.expire(
                i.id, actor="queue", reason="expired: not decided within 24 hours", now=now)]


def latest_open(store: st.Store, *, channel_id: int, agent_id: str) -> st.Item | None:
    """The most recent undecided action this agent staged in this channel."""
    mine = [i for i in store.items(states=(st.OPEN, st.SNOOZED), kinds=(st.ACTION,))
            if i.agent == agent_id and int(i.payload.get("channel_id") or 0) == int(channel_id)]
    return mine[-1] if mine else None
