"""Inbound attachment ingestion — files dropped to an agent via Discord DMs/mentions.

This is the INBOUND lane. It's distinct from `attachments.py`, which handles
OUTBOUND attachments (agents emitting an ```attachments``` block to send files
to Discord). The two lanes don't share code or denylists because their
threat models are different:

- OUTBOUND: agent says "send /Users/.../foo.pem to Discord". We must
  refuse to exfiltrate credentials. Path-prefix denylist over $HOME.
- INBOUND: Andy drops a file into a Discord chat with an agent. We
  download it, save it inside the vault under the agent's drop dir
  (`Andy Profile/dropped-files/` for Echo, `Luna/dropped-files/` for
  Luna), extract text from binary doc formats so the Read tool can
  consume them, and pass the paths back to the caller so they can be
  injected into the agent's mention prompt.

Why inside the vault: each ingest-capable agent already has read access
to the vault root via ADD_DIRS_PER_AGENT, so anything we drop under the
agent's namespace is automatically reachable without a separate --add-dir.
The drop dir lives inside the agent's writable subdirectory so dropping
a file is, from a sandbox-permissions perspective, indistinguishable from
the agent writing a note herself.

Supported types are per-agent (see `ALLOWED_EXTENSIONS_PER_AGENT`):
    .txt, .md       → saved as-is. Read tool handles natively.
    .pdf            → saved as-is. Read tool handles natively.
    .eml            → saved as-is, PLUS a `.txt` sidecar with extracted text.
    .docx           → saved as-is, PLUS a `.txt` sidecar with extracted text.
    image formats   → saved as-is. Read tool handles natively (multimodal).
                      .png, .jpg, .jpeg, .gif, .webp, .heic. No sidecar —
                      Claude Code's Read tool loads the image directly.

The sidecar text files (for .docx/.eml) are what the agent will read
first; the originals are kept alongside in case Andy wants to look at
them in Obsidian/Finder. Images have no sidecar; the agent reads the
image file itself.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import email
import email.policy
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

_logger = logging.getLogger("nb_discord.ingest")

# Per-agent ingest config. Each agent that supports inbound attachments gets
# its own drop directory (inside the agent's writable vault subdir) and its
# own allowed-extensions set (Echo is text-corpus; Luna also handles images
# because she's the assistant Andy actually screenshots things to).
_VAULT_ROOT = Path.home() / "Documents" / "Luna Master"

_DOC_EXTENSIONS = {".txt", ".md", ".pdf", ".eml", ".docx"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}

ALLOWED_EXTENSIONS_PER_AGENT: dict[str, set[str]] = {
    "echo": _DOC_EXTENSIONS,
    "luna": _DOC_EXTENSIONS | _IMAGE_EXTENSIONS,
}

DROPPED_FILES_DIR_PER_AGENT: dict[str, Path] = {
    "echo": _VAULT_ROOT / "Andy Profile" / "dropped-files",
    "luna": _VAULT_ROOT / "Luna" / "dropped-files",
}

# Back-compat aliases. `DROPPED_FILES_DIR` is what older tests monkey-patch;
# `ALLOWED_EXTENSIONS` is what the handlers gate filtered on before per-agent
# routing landed. New code should use the per-agent accessors below.
DROPPED_FILES_DIR = DROPPED_FILES_DIR_PER_AGENT["echo"]
ALLOWED_EXTENSIONS = set().union(*ALLOWED_EXTENSIONS_PER_AGENT.values())


def allowed_extensions_for(agent_id: str) -> set[str]:
    """Return the set of accepted file extensions for `agent_id`, or empty set
    if this agent isn't wired for inbound ingest."""
    return ALLOWED_EXTENSIONS_PER_AGENT.get(agent_id, set())


def dropped_files_dir_for(agent_id: str) -> Path:
    """Return the drop directory for `agent_id`. Falls back to the Echo
    default for unknown agents so we never write outside the vault, though
    handlers.py won't call us for an un-wired agent."""
    return DROPPED_FILES_DIR_PER_AGENT.get(agent_id, DROPPED_FILES_DIR)

# Discord caps non-Nitro uploads at 25 MB; we accept up to that. The OUTBOUND
# 24 MB cap is about Discord rejecting OUR uploads, not theirs.
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 5

# Soft cap on .docx/.eml extracted text to keep Echo's context budget sane.
# Anything past this gets a truncation marker; the original file remains intact
# on disk so Echo can pull more via the Read tool if needed.
MAX_EXTRACTED_CHARS = 250_000


@dataclass
class IngestedFile:
    """One successfully saved attachment + (optionally) its extracted text sidecar."""
    original_filename: str
    saved_path: Path
    sidecar_text_path: Path | None = None
    size_bytes: int = 0
    extracted_chars: int | None = None  # populated when a sidecar was written


@dataclass
class IngestResult:
    """Result of processing all attachments on one Discord message."""
    ingested: list[IngestedFile] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (filename, reason)
    over_cap: bool = False


# ---------- filename sanitization ----------

_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str, *, max_len: int = 120) -> str:
    """Reduce an arbitrary user-supplied filename to a safe basename.

    Strips path separators, control chars, and exotic Unicode. Keeps the
    extension if recognizable. Never returns an empty string; falls back to
    `dropped-file` if everything was stripped.
    """
    # Strip any directory parts; Discord shouldn't include them but be defensive.
    name = Path(name).name
    # Replace runs of unsafe chars with a single dash.
    safe = _UNSAFE_NAME_RE.sub("-", name).strip("-._")
    if not safe:
        safe = "dropped-file"
    if len(safe) > max_len:
        # Preserve the suffix.
        stem, _, suffix = safe.rpartition(".")
        if suffix and len(suffix) <= 10:
            keep = max_len - len(suffix) - 1
            safe = stem[:keep] + "." + suffix
        else:
            safe = safe[:max_len]
    return safe


def _unique_target(directory: Path, basename: str) -> Path:
    """Return directory/basename, suffixing -2, -3, ... if needed to avoid collision."""
    target = directory / basename
    if not target.exists():
        return target
    stem, _, suffix = basename.rpartition(".")
    if not suffix:
        stem, suffix = basename, ""
    i = 2
    while True:
        candidate = f"{stem}-{i}" + (f".{suffix}" if suffix else "")
        target = directory / candidate
        if not target.exists():
            return target
        i += 1


# ---------- format-specific text extraction ----------

# .docx XML namespaces. Only the WordprocessingML one matters for body text.
_DOCX_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCX_T_TAG = f"{{{_DOCX_W_NS}}}t"
_DOCX_P_TAG = f"{{{_DOCX_W_NS}}}p"


def extract_docx_text(docx_path: Path) -> str:
    """Pull paragraph text out of a .docx (Open XML) file.

    Reads `word/document.xml` from the zip, walks the WordprocessingML tree,
    joins `<w:t>` runs within each `<w:p>` (paragraph), and emits one paragraph
    per line. Tables, headers, footers, comments, and tracked changes are
    intentionally skipped — this is a personality-corpus extractor, not a
    fidelity-preserving converter.
    """
    try:
        with zipfile.ZipFile(docx_path) as zf:
            with zf.open("word/document.xml") as f:
                tree = ET.parse(f)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"docx_parse_failed: {type(exc).__name__}: {exc}") from exc

    root = tree.getroot()
    paragraphs: list[str] = []
    for p in root.iter(_DOCX_P_TAG):
        runs = [t.text for t in p.iter(_DOCX_T_TAG) if t.text]
        if runs:
            paragraphs.append("".join(runs))
        else:
            # Empty paragraph — preserve the blank line so paragraph rhythm stays intact.
            paragraphs.append("")
    return "\n".join(paragraphs).strip()


def extract_eml_text(eml_path: Path) -> str:
    """Pull the human-readable body out of a .eml RFC-5322 message.

    Strategy:
      - Parse with `email.policy.default` so we get the modern EmailMessage API.
      - Emit a small header block (From / To / Subject / Date).
      - Walk the message; for the first `text/plain` part (or `text/html`
        fallback stripped of tags) include the body. Skip attachments.
    """
    with eml_path.open("rb") as f:
        msg = email.message_from_binary_file(f, policy=email.policy.default)

    headers: list[str] = []
    for hdr in ("From", "To", "Cc", "Subject", "Date"):
        val = msg.get(hdr)
        if val:
            headers.append(f"{hdr}: {val}")

    body_parts: list[str] = []
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        try:
            text = body.get_content()
        except (LookupError, UnicodeDecodeError):
            # Fall back to raw payload as best-effort string.
            payload = body.get_payload(decode=True)
            text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
        if body.get_content_type() == "text/html":
            text = _strip_html(text)
        body_parts.append(text.strip())

    parts = []
    if headers:
        parts.append("\n".join(headers))
    if body_parts:
        parts.append("\n\n".join(body_parts))
    return "\n\n".join(parts).strip()


# Very minimal HTML-to-text. We're not trying to render the email; we're trying
# to give Echo enough of the prose to study voice. Strip tags, decode a handful
# of common entities, collapse whitespace.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_WHITESPACE_RE = re.compile(r"[ \t]+")
_HTML_NEWLINE_RE = re.compile(r"\n{3,}")
_HTML_ENTITY_MAP = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&apos;": "'",
}


def _strip_html(html: str) -> str:
    text = html
    for entity, replacement in _HTML_ENTITY_MAP.items():
        text = text.replace(entity, replacement)
    # Convert <br>, </p>, </div> to newlines BEFORE stripping all tags so paragraph rhythm survives.
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*(p|div|li|h[1-6])\s*>", "\n", text, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub("", text)
    text = _HTML_WHITESPACE_RE.sub(" ", text)
    text = _HTML_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


# ---------- the main entry point ----------

async def ingest_attachments(message, *, agent_id: str) -> IngestResult:
    """Download every supported attachment on `message`, save under the
    agent's `dropped-files/YYYY-MM-DD/`, and extract text sidecars for
    .docx/.eml. Images are saved as-is for the Read tool to consume.

    `message` is a discord.Message. We type it loosely to keep this module
    test-importable without the discord dependency (the tests mock attachments).

    `agent_id` selects both the allowed-extensions set and the drop dir.
    If the agent isn't wired for ingest, every attachment is rejected with
    `agent_not_wired_for_ingest`; handlers.py shouldn't call us in that
    case, but we guard defensively.
    """
    attachments = list(getattr(message, "attachments", []) or [])
    result = IngestResult()

    if not attachments:
        return result

    if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        result.over_cap = True
        attachments = attachments[:MAX_ATTACHMENTS_PER_MESSAGE]

    allowed = allowed_extensions_for(agent_id)
    if not allowed:
        for att in attachments:
            name = getattr(att, "filename", None) or "untitled"
            result.rejected.append((name, "agent_not_wired_for_ingest"))
            _logger.info("INGEST reject (agent): agent=%s name=%r", agent_id, name)
        return result

    today = _dt.date.today().isoformat()
    day_dir = dropped_files_dir_for(agent_id) / today
    day_dir.mkdir(parents=True, exist_ok=True)

    for att in attachments:
        original_name = getattr(att, "filename", None) or "untitled"
        size_bytes = int(getattr(att, "size", 0) or 0)
        ext = Path(original_name).suffix.lower()

        if ext not in allowed:
            result.rejected.append((original_name, f"unsupported_extension:{ext or '<none>'}"))
            _logger.info("INGEST reject (ext): agent=%s name=%r ext=%s", agent_id, original_name, ext)
            continue

        if size_bytes > MAX_FILE_BYTES:
            mb = size_bytes / (1024 * 1024)
            result.rejected.append((original_name, f"too_large:{mb:.1f}MB"))
            _logger.info("INGEST reject (size): agent=%s name=%r %.1fMB", agent_id, original_name, mb)
            continue

        safe_name = sanitize_filename(original_name)
        target = _unique_target(day_dir, safe_name)

        try:
            await att.save(target)
        except Exception as exc:
            result.rejected.append((original_name, f"download_failed:{type(exc).__name__}"))
            _logger.warning(
                "INGEST download failed: agent=%s name=%r err=%s: %s",
                agent_id, original_name, type(exc).__name__, exc,
            )
            continue

        # Verify the file actually landed and re-stat for the real byte count.
        try:
            real_size = target.stat().st_size
        except OSError as exc:
            result.rejected.append((original_name, f"stat_failed:{exc}"))
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        if real_size == 0:
            result.rejected.append((original_name, "empty_file"))
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        sidecar_path: Path | None = None
        extracted_chars: int | None = None
        if ext in (".docx", ".eml"):
            try:
                if ext == ".docx":
                    extracted = await asyncio.to_thread(extract_docx_text, target)
                else:
                    extracted = await asyncio.to_thread(extract_eml_text, target)
            except ValueError as exc:
                # Extraction failed but we still keep the raw file on disk. Echo
                # won't get text she can read, so surface the failure.
                result.rejected.append((original_name, f"extract_failed:{exc}"))
                _logger.warning(
                    "INGEST extract failed (raw kept): agent=%s name=%r err=%s",
                    agent_id, original_name, exc,
                )
                # Fall through with no sidecar but the raw file present.
                extracted = ""
            except Exception as exc:  # noqa: BLE001 - report anything else without crashing the loop
                result.rejected.append((original_name, f"extract_error:{type(exc).__name__}"))
                _logger.exception(
                    "INGEST extract crashed: agent=%s name=%r", agent_id, original_name,
                )
                extracted = ""

            if extracted:
                if len(extracted) > MAX_EXTRACTED_CHARS:
                    extracted = (
                        extracted[:MAX_EXTRACTED_CHARS].rstrip()
                        + f"\n\n[truncated — original is {real_size} bytes at {target}]"
                    )
                sidecar_path = target.with_suffix(target.suffix + ".txt")
                sidecar_path.write_text(extracted, encoding="utf-8")
                extracted_chars = len(extracted)

        result.ingested.append(
            IngestedFile(
                original_filename=original_name,
                saved_path=target,
                sidecar_text_path=sidecar_path,
                size_bytes=real_size,
                extracted_chars=extracted_chars,
            )
        )
        _logger.info(
            "INGEST ok: agent=%s name=%r -> %s sidecar=%s",
            agent_id, original_name, target, sidecar_path,
        )

    return result


def format_prompt_block(result: IngestResult) -> str:
    """Render the ingested files as a markdown block to prepend to a mention prompt.

    Returns "" if no files were ingested. The agent sees concrete paths it
    can pass to the Read tool. For .docx/.eml the `.txt` sidecar leads
    (extracted prose) and the original is a sub-bullet. For images and
    other directly-readable formats, the saved path is the Read target —
    the Read tool handles PNG/JPG/etc. natively as multimodal input.
    """
    if not result.ingested:
        return ""

    lines = ["## Andy dropped files into this conversation\n"]
    lines.append(
        "He attached the file(s) below for you to study. Read them with the "
        "`Read` tool. For `.docx` and `.eml` files, the `.txt` sidecar is the "
        "extracted prose — read that. For images (PNG/JPG/etc.) and plain "
        "text, read the saved path directly; the Read tool loads images as "
        "visual input.\n"
    )
    for f in result.ingested:
        readable = f.sidecar_text_path or f.saved_path
        lines.append(
            f"- **{f.original_filename}** ({f.size_bytes:,} bytes) "
            f"→ Read: `{readable}`"
        )
        if f.sidecar_text_path is not None:
            lines.append(f"  - Original (binary): `{f.saved_path}`")
    lines.append("")
    return "\n".join(lines)
