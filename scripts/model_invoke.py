#!/usr/bin/env python3
"""model_invoke.py - provider-fallback for text-only LLM calls (Fugu Layer 2).

The whole substrate calls the Claude CLI (`claude -p`) on Andy's subscription.
That is a single-vendor dependency: an Anthropic outage, rate-limit, or access
restriction takes the 24/7 memory pipeline down with no recourse. This module
adds a fallback provider so the text-only cron path (the filing gate and concept
writer in compile.py) can keep running when the primary is unavailable.

The primary provider stays the Claude CLI and is unchanged. Callers run it
themselves (so existing subprocess mocks keep working) and only reach for the
fallback here when the primary fails at the provider level (timeout, non-zero
exit, or a missing CLI). Output validation (JSON, emptiness) stays with the
caller.

The fallback is an OpenAI-compatible /chat/completions endpoint configured by
environment, so one implementation covers OpenAI, OpenRouter, Azure, a local
Ollama (/v1), or a LiteLLM gateway:

    NB_FALLBACK_BASE_URL   e.g. https://openrouter.ai/api/v1   (trailing slash optional)
    NB_FALLBACK_API_KEY    bearer token (omit for a keyless local endpoint)
    NB_FALLBACK_MODEL      the fallback model id, e.g. openai/gpt-4o-mini

If NB_FALLBACK_BASE_URL is unset there is no fallback and the chain is
Claude-only, identical to the previous behavior. No third-party dependencies:
this uses only the standard library.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def fallback_config(env: dict | None = None) -> dict | None:
    """Return the fallback provider config from env, or None if not configured."""
    env = env if env is not None else os.environ
    base = (env.get("NB_FALLBACK_BASE_URL") or "").strip().rstrip("/")
    if not base:
        return None
    return {
        "base_url": base,
        "api_key": (env.get("NB_FALLBACK_API_KEY") or "").strip(),
        "model": (env.get("NB_FALLBACK_MODEL") or "").strip(),
    }


def fallback_available(env: dict | None = None) -> bool:
    return fallback_config(env) is not None


def parse_openai_response(raw: str) -> tuple[bool, str, str]:
    """Pull the assistant text out of an OpenAI-compatible chat completion."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False, "", "fallback_bad_json"
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return False, "", "fallback_no_content"
    if not isinstance(text, str) or not text.strip():
        return False, "", "fallback_empty"
    return True, text, ""


def _build_request(cfg: dict, prompt: str) -> urllib.request.Request:
    payload = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    return urllib.request.Request(
        cfg["base_url"] + "/chat/completions",
        data=payload, headers=headers, method="POST",
    )


def fallback_text(prompt: str, timeout: int, env: dict | None = None) -> tuple[bool, str, str]:
    """Run the prompt against the configured fallback provider.

    Returns (ok, text, error). When no fallback is configured the result is
    (False, "", "no_fallback_configured"), which callers fold into their error
    string. Never raises: network and decode failures become error strings.
    """
    cfg = fallback_config(env)
    if not cfg:
        return False, "", "no_fallback_configured"
    if not cfg["model"]:
        return False, "", "fallback_model_unset"

    req = _build_request(cfg, prompt)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return False, "", f"http_{exc.code}"
    except urllib.error.URLError as exc:
        return False, "", f"urlerror:{getattr(exc, 'reason', '')}"[:80]
    except Exception as exc:  # socket timeout and anything else: stay non-fatal
        return False, "", f"fallback_error:{type(exc).__name__}"

    return parse_openai_response(raw)
