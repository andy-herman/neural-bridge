"""Google OAuth for Luna's executive-assistant tools. Stdlib only.

No google-api-python-client. The rest of this codebase reaches HTTP APIs with
`urllib` (model_invoke, the Discord notifier, the Telegram sender), Andy's
CLAUDE.md asks for supply-chain diligence before any new dependency, and the
three calls needed here are a token refresh and two GETs. A 40MB SDK is not
worth it for that.

WHY THIS EXISTS AT ALL: Luna's charter says she owns Andy's calendar and inbox.
Her tool allowlist hand-enumerates eighteen `mcp__claude_ai_*` names for
Calendar, Gmail and Drive. None of them resolve, because `--allowedTools` only
auto-approves tools that already exist and claude.ai connectors are not loaded
in a headless `claude -p`. Tested 2026-08-15: she replies NO_CALENDAR_ACCESS.
This module plus the two CLIs beside it are the working substitute, following
the pattern that already works for Loid and `synapse-journal`: a real CLI, and
Bash scoped to it.

SCOPES, deliberately minimal:

    calendar.readonly   read events
    gmail.readonly      read mail

Both are read-only. Google has no draft-without-send scope: `gmail.compose`
grants drafting AND sending in the same grant. Andy's charter for Luna is
explicit that Gmail is draft-only and she never sends, so the send capability
is not requested by default. Adding compose is a deliberate opt-in documented
in GOOGLE_SETUP.md, and even then no CLI here exposes a send command.

Never logs or prints a token. A partial access token was leaked into a session
transcript on 2026-08-15 by a redaction filter that only matched
token/secret/key/refresh; `redact()` below exists so that cannot recur.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib import error, parse, request

CONFIG_DIR = Path(
    os.environ.get("NB_GOOGLE_CONFIG_DIR", Path.home() / ".config" / "neural-bridge")
)
CLIENT_SECRET_PATH = CONFIG_DIR / "google_client_secret.json"
TOKEN_PATH = CONFIG_DIR / "google_token.json"

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# Installed-app loopback. Google deprecated the OOB flow, so setup runs a
# one-shot local listener rather than asking Andy to paste a code.
REDIRECT_URI = "http://localhost:8765/"

READ_SCOPES = (
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
)
# Opt-in only. Grants send as well as draft; see the module docstring.
COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"

# Refresh a little early so a long-running command cannot expire mid-flight.
EXPIRY_SKEW_SECONDS = 120


class GoogleAuthError(RuntimeError):
    """Raised with an operator-readable reason. Never contains a token."""


def redact(value: str, keep: int = 4) -> str:
    """Render a secret safe to print. Used in every diagnostic path here."""
    if not value:
        return "<empty>"
    return f"<redacted:{len(value)} chars ending {value[-keep:]}>" if len(value) > keep else "<redacted>"


def is_configured() -> bool:
    return TOKEN_PATH.exists() and CLIENT_SECRET_PATH.exists()


def config_status() -> str:
    """One line an agent can read to understand what is missing."""
    if not CLIENT_SECRET_PATH.exists():
        return (f"not configured: no client secret at {CLIENT_SECRET_PATH}. "
                f"See scripts/luna/GOOGLE_SETUP.md")
    if not TOKEN_PATH.exists():
        return (f"not authorized: client secret present but no token at {TOKEN_PATH}. "
                f"Run: python -m scripts.luna.google_auth setup")
    return "configured"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GoogleAuthError(f"missing file: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        raise GoogleAuthError(f"unreadable {path.name}: {type(exc).__name__}")


def _client_credentials() -> tuple[str, str]:
    raw = _load_json(CLIENT_SECRET_PATH)
    # Google hands out {"installed": {...}} or {"web": {...}}.
    block = raw.get("installed") or raw.get("web") or raw
    cid, secret = block.get("client_id"), block.get("client_secret")
    if not cid or not secret:
        raise GoogleAuthError("client secret file has no client_id/client_secret")
    return cid, secret


def _post_token(payload: dict) -> dict:
    body = parse.urlencode(payload).encode("utf-8")
    req = request.Request(TOKEN_URL, data=body, method="POST")
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error_description", "")
        except Exception:
            pass
        raise GoogleAuthError(f"token endpoint returned {exc.code}: {detail or 'no detail'}")
    except (error.URLError, TimeoutError, OSError) as exc:
        raise GoogleAuthError(f"token endpoint unreachable: {type(exc).__name__}")


def _save_token(tok: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    # Tokens are credentials; keep them off other users on a shared machine.
    try:
        TOKEN_PATH.chmod(0o600)
    except OSError:
        pass


def access_token() -> str:
    """Return a valid access token, refreshing when needed. Never printed."""
    tok = _load_json(TOKEN_PATH)
    expires_at = float(tok.get("expires_at", 0))
    if tok.get("access_token") and expires_at - EXPIRY_SKEW_SECONDS > time.time():
        return tok["access_token"]

    refresh = tok.get("refresh_token")
    if not refresh:
        raise GoogleAuthError(
            "token file has no refresh_token; re-run "
            "`python -m scripts.luna.google_auth setup`"
        )
    cid, secret = _client_credentials()
    fresh = _post_token({
        "client_id": cid,
        "client_secret": secret,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    })
    if "access_token" not in fresh:
        raise GoogleAuthError("refresh succeeded but returned no access_token")
    tok["access_token"] = fresh["access_token"]
    tok["expires_at"] = time.time() + float(fresh.get("expires_in", 3600))
    # Google only re-issues a refresh token occasionally; keep the old one.
    if fresh.get("refresh_token"):
        tok["refresh_token"] = fresh["refresh_token"]
    _save_token(tok)
    return tok["access_token"]


def api_get(url: str, params: dict | None = None, timeout: int = 30) -> dict:
    """Authorized GET against a Google REST endpoint.

    `doseq` so a list value becomes a repeated query parameter, which Gmail
    requires for `metadataHeaders`: asking for From and Subject in one call
    rather than two.
    """
    if params:
        url = f"{url}?{parse.urlencode(params, doseq=True)}"
    req = request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {access_token()}")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        raise GoogleAuthError(f"API {exc.code}: {detail or url.split('?')[0]}")
    except (error.URLError, TimeoutError, OSError) as exc:
        raise GoogleAuthError(f"API unreachable: {type(exc).__name__}")


# ---------- one-time interactive setup ----------

def _run_setup(with_compose: bool) -> int:
    """Interactive OAuth. Andy runs this once; agents never do."""
    import http.server
    import socketserver
    import threading
    import webbrowser

    cid, secret = _client_credentials()
    scopes = list(READ_SCOPES) + ([COMPOSE_SCOPE] if with_compose else [])
    auth_url = f"{AUTH_URL}?" + parse.urlencode({
        "client_id": cid,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",  # force a refresh_token even on re-auth
    })

    captured: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = parse.parse_qs(parse.urlparse(self.path).query)
            captured.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            ok = "code" in captured
            self.wfile.write(b"Authorized. Close this tab." if ok
                             else b"No code returned. Check the console.")

        def log_message(self, *_args):
            return

    print("Scopes requested:")
    for s in scopes:
        print("  " + s)
    if with_compose:
        print("\nNOTE: gmail.compose also grants SEND. No CLI here exposes a send\n"
              "command, but the grant permits it. Skip --with-compose to stay read-only.")
    print(f"\nOpening the consent screen. If it does not open, visit:\n{auth_url}\n")

    with socketserver.TCPServer(("localhost", 8765), Handler) as httpd:
        threading.Thread(target=httpd.handle_request, daemon=True).start()
        webbrowser.open(auth_url)
        for _ in range(300):  # up to 5 minutes
            if "code" in captured or "error" in captured:
                break
            time.sleep(1)

    if "error" in captured:
        print(f"authorization failed: {captured['error']}")
        return 1
    if "code" not in captured:
        print("timed out waiting for the redirect")
        return 1

    tok = _post_token({
        "client_id": cid,
        "client_secret": secret,
        "code": captured["code"],
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    })
    if "refresh_token" not in tok:
        print("Google returned no refresh_token. Revoke the app at "
              "https://myaccount.google.com/permissions and retry.")
        return 1
    tok["expires_at"] = time.time() + float(tok.get("expires_in", 3600))
    tok["scopes"] = scopes
    _save_token(tok)
    print(f"\nAuthorized. Token written to {TOKEN_PATH} (mode 600).")
    print(f"refresh_token: {redact(tok['refresh_token'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="google_auth", description="Luna Google auth")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="one-time interactive authorization")
    s.add_argument("--with-compose", action="store_true",
                   help="also request gmail.compose (allows drafting; also grants send)")
    sub.add_parser("status", help="print configuration status")
    sub.add_parser("check", help="verify the token refreshes and the APIs answer")
    args = p.parse_args(argv)

    if args.cmd == "status":
        print(config_status())
        return 0 if is_configured() else 1
    if args.cmd == "setup":
        try:
            return _run_setup(args.with_compose)
        except GoogleAuthError as exc:
            print(f"setup failed: {exc}")
            return 1
    try:
        access_token()
        print("token OK (refreshed if needed)")
        return 0
    except GoogleAuthError as exc:
        print(f"check failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
