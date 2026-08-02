"""Honcho integration — shared peer-memory across all NB agents.

All agents talk to the same Honcho instance (default: http://localhost:8001),
within the same workspace ("hermes"), against the same user peer ("andyherman").
Each agent uses its own ID as its AI peer name, so Honcho's directional
observation mode keeps each agent's view of Andy distinct while the underlying
peer-card facts about Andy compound across all of them.

This module is a thin, lazy wrapper around the honcho-ai SDK. If Honcho is
unreachable or disabled, every public function degrades to a no-op (empty
context / silent submit). The bot keeps working; only the Honcho-derived
context is missing. Designed to be safe to add to every agent's pipeline.

Required: honcho-ai installed in the venv. If missing, all calls no-op with
a one-shot warning.

Environment:
  HONCHO_ENABLED     — "false" disables all calls (default: "true")
  HONCHO_BASE_URL    — Honcho API URL (default: http://localhost:8001)
  HONCHO_WORKSPACE   — workspace ID (default: hermes)
  HONCHO_USER_PEER   — user peer ID (default: andyherman)
  HONCHO_API_KEY     — auth token, empty for self-hosted no-auth (default: empty)
  HONCHO_TIMEOUT     — request timeout in seconds (default: 180)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

_logger = logging.getLogger("nb_discord.honcho")

_client_cache = None
_warned_unavailable = False
_warned_disabled = False
_logged_first_submit = False


def _enabled() -> bool:
    global _warned_disabled
    if os.environ.get("HONCHO_ENABLED", "true").lower() == "false":
        if not _warned_disabled:
            _logger.info("Honcho integration disabled via HONCHO_ENABLED=false")
            _warned_disabled = True
        return False
    return True


def _get_client():
    """Lazy-init the Honcho SDK client. Returns None on any failure."""
    global _client_cache, _warned_unavailable
    if _client_cache is not None:
        return _client_cache
    try:
        from honcho import Honcho
    except ImportError:
        if not _warned_unavailable:
            _logger.warning(
                "honcho-ai SDK not installed in venv — Honcho integration inactive. "
                "Install with: pip install honcho-ai"
            )
            _warned_unavailable = True
        return None
    try:
        base_url = os.environ.get("HONCHO_BASE_URL", "http://localhost:8001")
        workspace = os.environ.get("HONCHO_WORKSPACE", "hermes")
        api_key = os.environ.get("HONCHO_API_KEY") or "self-hosted"
        timeout = float(os.environ.get("HONCHO_TIMEOUT", "180"))
        _client_cache = Honcho(
            workspace_id=workspace,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
        _logger.info("Honcho client initialized (workspace=%s, base=%s)", workspace, base_url)
        return _client_cache
    except Exception as exc:
        if not _warned_unavailable:
            _logger.warning("Honcho client init failed: %s — integration inactive", exc)
            _warned_unavailable = True
        return None


def _user_peer_id() -> str:
    return os.environ.get("HONCHO_USER_PEER", "andyherman")


def get_peer_card_context(agent_id: str, max_chars: int = 2000) -> str:
    """Fetch the peer card context for andyherman from the agent's perspective.

    Returns a formatted markdown block suitable for prepending to a prompt, or
    an empty string if Honcho is unavailable / returns nothing. The agent's ID
    (e.g. "luna") is used as the AI peer name so observations attribute correctly.

    Truncates to max_chars to bound prompt growth.
    """
    if not _enabled():
        return ""
    client = _get_client()
    if client is None:
        return ""
    try:
        user_peer = _user_peer_id()
        # Pull this agent's directional view of Andy first; fall back to the
        # global andyherman peer card. Empty (None) when the deriver hasn't
        # yet produced facts — returns "" so the prompt is unchanged.
        agent_peer = client.peer(agent_id)
        card = agent_peer.get_card(user_peer) or client.peer(user_peer).get_card()
        if not card:
            return ""
        if isinstance(card, list):
            body = "\n".join(f"- {fact}" for fact in card if fact)
        else:
            body = str(card)
        if len(body) > max_chars:
            body = body[: max_chars - 3] + "..."
        return (
            "## Honcho peer card — what you (the agent) currently know about Andy\n\n"
            "These are persistent observations accumulated across all your conversations "
            "with Andy (and across other agents in this workspace). Use this to ground "
            "your reply; do not quote it back to him.\n\n"
            f"{body}\n"
        )
    except Exception as exc:
        # warning, not debug: a silent failure here hid a dead capture path for
        # two months (May-Aug 2026) — visibility is worth the occasional noise.
        _logger.warning("Honcho peer card fetch failed for agent %s: %s", agent_id, exc)
        return ""


def submit_turn(
    agent_id: str,
    user_message: str,
    agent_response: str,
    session_id: Optional[str] = None,
) -> None:
    """Submit a (user, agent) turn to Honcho for async deriver processing.

    Fire-and-forget — exceptions are logged at debug level only so a Honcho
    outage cannot break message handling. Call this AFTER the agent has
    successfully replied to Discord.

    session_id should be stable per Discord channel × agent pair so Honcho
    groups related turns. The bot's existing session_store UUIDs work well.
    """
    if not _enabled():
        return
    client = _get_client()
    if client is None:
        return
    try:
        user_peer = _user_peer_id()
        sid = session_id or f"{agent_id}-default"
        session = client.session(sid)
        session.add_messages(
            [
                {"peer_id": user_peer, "content": user_message},
                {"peer_id": agent_id, "content": agent_response},
            ]
        )
        global _logged_first_submit
        if not _logged_first_submit:
            _logger.info("Honcho first turn submitted OK (agent=%s, session=%s)", agent_id, sid[:8])
            _logged_first_submit = True
    except Exception as exc:
        # warning, not debug: a silent failure here hid a dead capture path for
        # two months (May-Aug 2026) — visibility is worth the occasional noise.
        _logger.warning("Honcho turn submit failed for agent %s: %s", agent_id, exc)


def ensure_peer(agent_id: str) -> None:
    """Idempotently create the AI peer for this agent if it doesn't exist.

    Safe to call repeatedly (e.g. on every mention). The SDK's peer() call
    creates on-demand, so we just touch both peers to make sure they exist.
    """
    if not _enabled():
        return
    client = _get_client()
    if client is None:
        return
    try:
        client.peer(_user_peer_id())
        client.peer(agent_id)
    except Exception as exc:
        _logger.debug("Honcho peer ensure failed for %s: %s", agent_id, exc)
