"""Shared pytest setup for hooks/ and scripts/.

hooks/claude_env.route() picks the API route whenever a key is configured,
and the Mac Mini has one in ~/.config/neural-bridge. Without this, a test's
route would depend on the machine it runs on. Every test starts on the proxy
route with no key; tests of the API route set NB_ANTHROPIC_API_KEY themselves.

Every `claude -p` call also appends a line to the fleet's usage log
(hooks/claude_usage.py). Tests fake those calls by the hundred, so each test
gets its own usage folder, never the real one.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_ambient_model_route(monkeypatch):
    monkeypatch.delenv("NB_MODEL_ROUTE", raising=False)
    monkeypatch.delenv("NB_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("NB_ANTHROPIC_KEY_FILE", "/nonexistent/nb-test-anthropic-api-key")


@pytest.fixture(autouse=True)
def _no_real_usage_log(monkeypatch, tmp_path):
    import sys
    from pathlib import Path
    hooks = str(Path(__file__).resolve().parent / "hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    import claude_usage
    monkeypatch.setattr(claude_usage, "USAGE_DIR", tmp_path / "nb-usage")

