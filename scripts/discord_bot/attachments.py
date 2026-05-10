"""File-attachment protocol for mentioned agents.

Agents emit a single fenced ` ```attachments ` block at the end of their
response containing a JSON array of absolute file paths. The daemon extracts
the block, validates each path, and uses `discord.File` to attach the files
to the posted message. The block is stripped from the visible reply.

Why a separate block (not folded into `actions`):
- `actions` are GitHub-side operations executed via `gh` and reported as
  a separate summary message. Attachments are a Discord post mechanic
  that ride along with the agent's reply. Different lanes.

Validation rules:
- Path must be absolute and resolve under $HOME (after symlink resolution
  to defeat `~/Documents/foo -> /etc/passwd` style escapes).
- Path must NOT match the credential denylist (~/.ssh, ~/.aws, ~/.gnupg,
  ~/.config/gh, ~/.config/git, ~/.kube, ~/.docker, ~/.netrc, ~/.gitconfig,
  ~/.git/, .env*, id_rsa*, *.pem, *.key, .zsh_history, .bash_history).
- File must exist, be a regular file, be non-empty, and be ≤ 24 MB
  (Discord's 25 MB limit minus 1 MB headroom for the message itself).

Per-message cap: 5 attachments. Same shape as MAX_ACTIONS_PER_MENTION.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
MAX_BYTES = 24 * 1024 * 1024  # 24 MB
MAX_ATTACHMENTS_PER_MESSAGE = 5

ATTACHMENT_BLOCK_RE = re.compile(
    r"```\s*attachments?\s*\n(.*?)\n\s*```",
    re.DOTALL | re.IGNORECASE,
)

# Denylist: directories under HOME we never serve from. Each entry is a
# path relative to HOME; the validator rejects any file whose resolved
# path begins with one of these segments.
DENY_DIRS = [
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".config/gh",
    ".config/git",
    ".git",  # any .git/ anywhere in the path
]

# Denylist: basename patterns. Applied against the file's basename only.
DENY_BASENAME_PATTERNS = [
    re.compile(r"^\.env(\..*)?$"),       # .env, .env.local, .env.production, etc.
    re.compile(r"^id_rsa(\..*)?$"),      # id_rsa, id_rsa.pub
    re.compile(r"^id_ed25519(\..*)?$"),  # id_ed25519, id_ed25519.pub
    re.compile(r"\.pem$"),
    re.compile(r"\.key$"),
    re.compile(r"^\.netrc$"),
    re.compile(r"^\.gitconfig$"),
    re.compile(r"^\.zsh_history$"),
    re.compile(r"^\.bash_history$"),
    re.compile(r"^\.python_history$"),
]


@dataclass
class ParsedAttachmentBlock:
    """Result of extract_attachments: the response with the attachments
    block stripped, and the parsed paths (or None if no block / parse error).

    `parse_error` is set if a block was found but malformed; the visible
    response in that case is the unmodified original."""
    visible_response: str
    paths: list[str] | None
    parse_error: str | None


def extract_attachments(response_text: str) -> ParsedAttachmentBlock:
    """Find the attachments block, strip it from the response, parse the JSON."""
    match = ATTACHMENT_BLOCK_RE.search(response_text)
    if not match:
        return ParsedAttachmentBlock(
            visible_response=response_text,
            paths=None,
            parse_error=None,
        )

    raw_json = match.group(1).strip()
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        return ParsedAttachmentBlock(
            visible_response=response_text,
            paths=None,
            parse_error=f"json_decode: {exc.msg}",
        )

    if not isinstance(parsed, list):
        return ParsedAttachmentBlock(
            visible_response=response_text,
            paths=None,
            parse_error="attachments block must be a JSON array of paths",
        )

    if not all(isinstance(p, str) for p in parsed):
        return ParsedAttachmentBlock(
            visible_response=response_text,
            paths=None,
            parse_error="attachments block must contain only string paths",
        )

    visible = (response_text[: match.start()] + response_text[match.end():]).strip()
    return ParsedAttachmentBlock(
        visible_response=visible,
        paths=parsed,
        parse_error=None,
    )


@dataclass
class PathValidationResult:
    ok: bool
    error: str | None = None


def validate_path(path_str: str, *, home: Path = HOME, max_bytes: int = MAX_BYTES) -> PathValidationResult:
    """Validate a single attachment path. See module docstring for rules."""
    if not isinstance(path_str, str) or not path_str.strip():
        return PathValidationResult(False, "empty_or_non_string_path")

    p = Path(path_str)
    if not p.is_absolute():
        return PathValidationResult(False, "path_must_be_absolute")

    try:
        resolved = p.resolve(strict=True)
    except FileNotFoundError:
        return PathValidationResult(False, "file_not_found")
    except (OSError, RuntimeError):
        return PathValidationResult(False, "path_resolution_failed")

    # Must be under $HOME after resolution (defeats symlink escape).
    home_resolved = home.resolve()
    try:
        relative = resolved.relative_to(home_resolved)
    except ValueError:
        return PathValidationResult(False, "outside_home_directory")

    # Must be a regular file (not directory, not socket, not device).
    if not resolved.is_file():
        return PathValidationResult(False, "not_a_regular_file")

    # Denylist by directory prefix.
    parts = relative.parts
    for deny_dir in DENY_DIRS:
        deny_parts = Path(deny_dir).parts
        # Match either as a path prefix (~/.ssh/key) or as any embedded
        # segment for the .git case (foo/.git/config).
        if parts[: len(deny_parts)] == deny_parts:
            return PathValidationResult(False, f"sensitive_dir:{deny_dir}")
        if deny_dir == ".git" and ".git" in parts:
            return PathValidationResult(False, "sensitive_dir:.git")

    # Denylist by basename pattern.
    basename = resolved.name
    for pattern in DENY_BASENAME_PATTERNS:
        if pattern.search(basename):
            return PathValidationResult(False, f"sensitive_filename:{basename}")

    # Size check.
    try:
        size = resolved.stat().st_size
    except OSError:
        return PathValidationResult(False, "stat_failed")
    if size == 0:
        return PathValidationResult(False, "empty_file")
    if size > max_bytes:
        mb = size / (1024 * 1024)
        return PathValidationResult(False, f"too_large:{mb:.1f}MB_exceeds_{max_bytes // (1024*1024)}MB")

    return PathValidationResult(True, None)


@dataclass
class ValidatedAttachments:
    """Outcome of validating a list of paths.

    `valid_paths` is the subset that passed (resolved to absolute Path objects).
    `errors` is a list of (path_str, reason) for paths that failed.
    `over_cap` is True if the input had more than MAX_ATTACHMENTS_PER_MESSAGE.
    """
    valid_paths: list[Path]
    errors: list[tuple[str, str]]
    over_cap: bool


def validate_attachment_batch(paths: list[str]) -> ValidatedAttachments:
    """Validate a list of paths. Returns sorted-out valid + errors.

    Hard cap of MAX_ATTACHMENTS_PER_MESSAGE — anything beyond is dropped
    with `over_cap=True` so the caller can surface the truncation."""
    over_cap = len(paths) > MAX_ATTACHMENTS_PER_MESSAGE
    paths = paths[:MAX_ATTACHMENTS_PER_MESSAGE]

    valid: list[Path] = []
    errors: list[tuple[str, str]] = []
    for path_str in paths:
        result = validate_path(path_str)
        if result.ok:
            valid.append(Path(path_str).resolve())
        else:
            errors.append((path_str, result.error or "unknown_error"))
    return ValidatedAttachments(valid_paths=valid, errors=errors, over_cap=over_cap)
