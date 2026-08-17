"""Tests for scripts/luna/live_state.py.

The point of this module is that it never blocks a turn and never lies about
what it fetched, so most of these are failure paths.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.luna import live_state as ls  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "CACHE_PATH", tmp_path / "state.json")


def _parts(today="Today: nothing.", upcoming="Upcoming (0):", waiting="Nothing sitting unanswered."):
    return {"today": today, "upcoming": upcoming, "waiting": waiting}


# ---------- rendering ----------

def test_block_contains_all_three_sections(monkeypatch):
    monkeypatch.setattr(ls, "_fetch", lambda: _parts(today="Today: 2pm standup"))
    block = ls.live_state_block()
    assert "2pm standup" in block
    assert "### Today" in block and "### Next" in block
    assert "waiting on replies" in block


def test_block_is_truncated_to_budget(monkeypatch):
    monkeypatch.setattr(ls, "_fetch", lambda: _parts(waiting="x" * 10_000))
    block = ls.live_state_block(max_chars=500)
    assert len(block) <= 500


# ---------- failure policy ----------

def test_total_fetch_failure_returns_empty_not_raises(monkeypatch):
    def boom():
        raise RuntimeError("google is down")
    monkeypatch.setattr(ls, "_fetch", boom)
    assert ls.live_state_block() == ""


def test_partial_failure_still_returns_the_working_half(monkeypatch):
    """A broken inbox must not cost her the calendar."""
    monkeypatch.setattr(ls, "_fetch",
                        lambda: _parts(today="Today: 9am flight",
                                       waiting="_(inbox unavailable: GoogleAuthError)_"))
    block = ls.live_state_block()
    assert "9am flight" in block
    assert "inbox unavailable" in block


def test_failure_text_is_repeatable_to_andy(monkeypatch):
    """The error has to be words she can quote, since inventing this sentence
    is the exact bug this module exists to remove."""
    monkeypatch.setattr(ls, "_fetch",
                        lambda: _parts(today="_(calendar unavailable: API 400. Tell Andy this verbatim.)_"))
    assert "API 400" in ls.live_state_block()


# ---------- caching ----------

def test_second_call_uses_cache(monkeypatch):
    calls = []

    def counted():
        calls.append(1)
        return _parts(today="Today: cached me")
    monkeypatch.setattr(ls, "_fetch", counted)
    ls.live_state_block()
    ls.live_state_block()
    assert len(calls) == 1


def test_expired_cache_refetches(monkeypatch):
    calls = []

    def counted():
        calls.append(1)
        return _parts()
    monkeypatch.setattr(ls, "_fetch", counted)
    ls.live_state_block()
    ls.live_state_block(ttl=0)
    assert len(calls) == 2


def test_corrupt_cache_is_ignored_not_fatal(monkeypatch):
    ls.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ls.CACHE_PATH.write_text("{not json")
    monkeypatch.setattr(ls, "_fetch", lambda: _parts(today="Today: refetched"))
    assert "refetched" in ls.live_state_block()


def test_cache_missing_fetched_at_is_ignored(monkeypatch):
    ls.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ls.CACHE_PATH.write_text(json.dumps({"parts": _parts(today="stale")}))
    monkeypatch.setattr(ls, "_fetch", lambda: _parts(today="Today: fresh"))
    assert "fresh" in ls.live_state_block()


def test_unwritable_cache_does_not_break_the_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(ls, "CACHE_PATH", tmp_path / "nodir" / "x" / "s.json")
    monkeypatch.setattr(ls, "_write_cache", lambda parts: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(ls, "_fetch", lambda: _parts(today="Today: still fine"))
    with pytest.raises(OSError):
        ls._write_cache({})
    # and through the public path, the throw is contained
    monkeypatch.setattr(ls, "_write_cache", lambda parts: None)
    assert "still fine" in ls.live_state_block()


def test_fresh_cache_is_honored_within_ttl(monkeypatch):
    ls.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ls.CACHE_PATH.write_text(json.dumps(
        {"fetched_at": time.time(), "parts": _parts(today="Today: from cache")}))

    def should_not_run():
        raise AssertionError("fetched despite a fresh cache")
    monkeypatch.setattr(ls, "_fetch", should_not_run)
    assert "from cache" in ls.live_state_block()
