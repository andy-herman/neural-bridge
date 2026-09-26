#!/usr/bin/env python3
"""Outbound guard: keep text from marked vault notes off public surfaces.

Agents read the Obsidian vault, and two routes publish what they write: the
wiki (compile.py writes knowledge/, which is tracked in this public repo) and
GitHub (the Discord daemon's issues, comments, PR branches and PR bodies).
Nothing upstream screens that text for confidentiality markings. This module
does, on both routes, and fails closed.

Method (the vault leak check's, in Gemma GRC scripts/leak_scan.py): every note
the corpus gate marks (non-public classification labels, private or
confidential flags and tags, handling banners), plus every note under the
private policy folders, is cut into 12-word shingles, from both its raw
markdown and its cleaned text. Outbound text is blocked when it shares
THRESHOLD or more shingle occurrences with that index, which takes a verbatim
run of about 14 words (shorter runs and paraphrase are below the floor), or
when it contains a private marking phrase. A missing, stale, corrupt or
mismatched index blocks everything.

One deliberate difference from the leak check: shingles that also occur in
unmarked notes are NOT discounted. Agents write unmarked notes (conversation
archives, session notes), so a discount would let a single quote of a marked
passage unprotect it at the next rebuild. Measured 2026-09-25, the discount
covered 789 shingles, mostly copies in agent-written notes, and prevented no
false positive on this repo or the published blog.

Nothing sensitive lives in this repo or in the index:
  - The marking phrases and policy folders sit in a private policy file
    outside the repo (default ~/.config/neural-bridge/outbound-guard.json,
    mode 600), read only when the index is built.
  - The index (data/outbound_guard/index.bin, gitignored) holds keyed
    blake2b 8-byte digests, never text. The key sits next to the policy file,
    so a stray copy of the index cannot be dictionary-attacked for the short
    marking phrases or used to confirm a guessed passage.
  - The audit log (data/outbound_guard/audit.jsonl) records counts and
    digests for every check, never text.

Usage:
  python scripts/outbound_guard.py build      # rebuild the index from the vault
  python scripts/outbound_guard.py status     # index age and counts
  python scripts/outbound_guard.py check < f  # screen a draft; counts only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import unicodedata
from array import array
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent

N = 12                     # words per shingle (leak_scan.py)
THRESHOLD = 3              # matching shingle occurrences that block (leak_scan.py)
MAX_AGE_HOURS = 24.0       # an older index blocks everything
REFRESH_AFTER_HOURS = 1.0  # the daemon rebuilds this often; compile rebuilds anything older
BUILD_TIMEOUT = 300        # seconds allowed for a rebuild subprocess

ENV_DIR = "NB_OUTBOUND_GUARD_DIR"        # index and audit log
ENV_POLICY = "NB_OUTBOUND_GUARD_POLICY"  # private policy JSON; the key file sits beside it
ENV_VAULT = "NB_OUTBOUND_GUARD_VAULT"
ENV_GATE = "NB_OUTBOUND_GUARD_GATE"      # directory holding Gemma GRC's vault_ingest.py

INDEX_FILE = "index.bin"
AUDIT_FILE = "audit.jsonl"
AUDIT_MAX_BYTES = 5 * 1024 * 1024
FORMAT = 1
MAGIC = b"NBOG1\n"
SKIP_PARTS = (".trash", ".obsidian")     # as leak_scan.py

CONTENT_REASONS = frozenset({"shingles", "marking", "shingles+marking"})
INDEX_REASONS = frozenset({"no-index", "stale-index", "bad-index", "no-key", "key-mismatch"})

if array("Q").itemsize != 8 or array("I").itemsize != 4:  # pragma: no cover
    raise ImportError("outbound_guard needs 8-byte 'Q' and 4-byte 'I' arrays")


# ---------- locations (read per call so tests and tools can redirect them) ----------

def state_dir() -> Path:
    return Path(os.environ.get(ENV_DIR) or REPO_ROOT / "data" / "outbound_guard")


def policy_path() -> Path:
    return Path(os.environ.get(ENV_POLICY)
                or Path.home() / ".config" / "neural-bridge" / "outbound-guard.json")


def key_path() -> Path:
    return policy_path().with_suffix(".key")


def vault_path() -> Path:
    return Path(os.environ.get(ENV_VAULT) or Path.home() / "Documents" / "Luna Master")


def gate_dir() -> Path:
    return Path(os.environ.get(ENV_GATE) or Path.home() / "Development" / "gemma-grc" / "scripts")


# ---------- errors ----------

class GuardError(Exception):
    """Build or policy problem. Messages never carry note text or policy content."""


class PolicyError(GuardError):
    pass


class BuildError(GuardError):
    pass


class IndexUnavailable(Exception):
    """The index cannot be used, so every check fails closed."""

    def __init__(self, reason: str, detail: str = "", age_hours: float | None = None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.age_hours = age_hours


# ---------- text -> digests (normalization and shingling exactly as leak_scan.py) ----------

_NON_WORD = re.compile(r"[^a-z0-9]+")


def norm_words(text: str) -> list[str]:
    """Lowercase alphanumeric words. Punctuation, markdown and line breaks vanish,
    so reformatting a passage does not hide it."""
    return _NON_WORD.sub(" ", text.lower()).split()


def _digest(key: bytes, phrase: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(phrase.encode("utf-8", "replace"), digest_size=8, key=key).digest(), "big")


def _shingles(words: list[str], key: bytes) -> Iterable[int]:
    for i in range(len(words) - N + 1):
        yield _digest(key, " ".join(words[i:i + N]))


def _ref(key: bytes, text: str) -> str:
    """Keyed digest of a whole outbound text: ties log lines about one item together."""
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=8, key=key,
                           person=b"nb-og-ref").hexdigest()


def _key_id(key: bytes) -> str:
    return hashlib.blake2b(b"", digest_size=8, key=key, person=b"nb-og-key-id").hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- verdicts ----------

@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str                       # "clear"/"ready", a CONTENT_REASONS value, or why the index is unusable
    shingle_hits: int = 0             # matching shingle occurrences (leak_scan.py's count)
    notes_hit: int = 0                # distinct marked notes those shingles come from
    categories: tuple[str, ...] = ()  # gate categories of those notes, e.g. "tag", "banner"
    marking_hits: int = 0
    ref: str = ""                     # keyed digest of the whole text
    index_age_hours: float | None = None

    def describe(self) -> str:
        """One line for operators and chat replies: counts and digests, never text."""
        ref = f" [ref {self.ref}]" if self.ref else ""
        if self.allowed:
            return f"outbound guard: {self.reason}{ref}"
        if self.reason in CONTENT_REASONS:
            cats = f" ({', '.join(self.categories)})" if self.categories else ""
            return (f"outbound guard blocked this text: {self.shingle_hits} shingle(s) from "
                    f"{self.notes_hit} marked note(s){cats}, {self.marking_hits} marking phrase(s){ref}")
        if self.reason in INDEX_REASONS:
            age = f", index {self.index_age_hours:.1f}h old" if self.index_age_hours is not None else ""
            return (f"outbound guard blocked this text: index unusable ({self.reason}{age}), failing closed; "
                    f"rebuild with `python scripts/outbound_guard.py build`{ref}")
        return f"outbound guard could not screen this text ({self.reason}), failing closed{ref}"

    def record(self, surface: str) -> dict:
        return {"ts": _utc_now(), "surface": surface, "allowed": self.allowed, "reason": self.reason,
                "shingle_hits": self.shingle_hits, "notes_hit": self.notes_hit,
                "categories": list(self.categories), "marking_hits": self.marking_hits,
                "ref": self.ref, "index_age_hours": self.index_age_hours}


class OutboundBlocked(Exception):
    """Raised by enforce(). Carries the verdict; its message is counts only."""

    def __init__(self, verdict: Verdict):
        super().__init__(verdict.describe())
        self.verdict = verdict


# ---------- the index ----------

def _lookup(arr: array, value: int) -> int:
    i = bisect_left(arr, value)
    return i if i < len(arr) and arr[i] == value else -1


class Index:
    def __init__(self, header: dict, key: bytes, digests: array, note_ids: array,
                 note_cats: bytes, marking: array):
        self.header = header
        self.key = key
        self.digests = digests        # sorted distinctive shingle digests
        self.note_ids = note_ids      # marked note each digest came from
        self.note_cats = note_cats    # category index per marked note
        self.marking = marking        # sorted marking-phrase digests
        self.categories = list(header["categories"])
        self.marking_lengths = [int(n) for n in header["marking_lengths"]]

    def age_hours(self, now: float | None = None) -> float:
        return ((now if now is not None else time.time()) - float(self.header["built_at"])) / 3600

    def check(self, text: str) -> Verdict:
        words = norm_words(text)
        per_note: Counter = Counter()
        for d in _shingles(words, self.key):
            i = _lookup(self.digests, d)
            if i >= 0:
                per_note[self.note_ids[i]] += 1
        marking_hits = 0
        for length in self.marking_lengths:
            for j in range(len(words) - length + 1):
                if _lookup(self.marking, _digest(self.key, " ".join(words[j:j + length]))) >= 0:
                    marking_hits += 1
        shingle_hits = sum(per_note.values())
        by_shingles = shingle_hits >= THRESHOLD
        by_marking = marking_hits > 0
        reason = ("shingles+marking" if by_shingles and by_marking else
                  "shingles" if by_shingles else "marking" if by_marking else "clear")
        return Verdict(
            allowed=reason == "clear",
            reason=reason,
            shingle_hits=shingle_hits,
            notes_hit=len(per_note),
            categories=tuple(sorted({self.categories[self.note_cats[n]] for n in per_note})),
            marking_hits=marking_hits,
            ref=_ref(self.key, text),
            index_age_hours=round(self.age_hours(), 2),
        )


def _le_bytes(a: array) -> bytes:
    a = array(a.typecode, a)
    if sys.byteorder == "big":  # pragma: no cover
        a.byteswap()
    return a.tobytes()


def _le_array(typecode: str, data: bytes) -> array:
    a = array(typecode)
    a.frombytes(data)
    if sys.byteorder == "big":  # pragma: no cover
        a.byteswap()
    return a


def _write_index(out: Path, header: dict, digests: array, note_ids: array,
                 note_cats: bytes, marking: array) -> None:
    """Write atomically, so a reader never sees half an index."""
    payload = _le_bytes(digests) + _le_bytes(note_ids) + bytes(note_cats) + _le_bytes(marking)
    header = dict(header, sizes=[len(digests), len(note_cats), len(marking)],
                  payload_blake2b=hashlib.blake2b(payload, digest_size=16).hexdigest())
    blob = MAGIC + json.dumps(header, sort_keys=True, separators=(",", ":")).encode() + b"\n" + payload
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


def _parse(blob: bytes) -> tuple[dict, bytes]:
    if not blob.startswith(MAGIC):
        raise IndexUnavailable("bad-index", "not an outbound-guard index")
    end = blob.find(b"\n", len(MAGIC))
    if end < 0:
        raise IndexUnavailable("bad-index", "truncated header")
    try:
        header = json.loads(blob[len(MAGIC):end])
    except ValueError:
        raise IndexUnavailable("bad-index", "unreadable header") from None
    if not isinstance(header, dict) or header.get("format") != FORMAT or header.get("n") != N:
        raise IndexUnavailable("bad-index", "format or shingle size differs from this code")
    return header, blob[end + 1:]


def load_key(path: Path | None = None) -> bytes:
    path = path or key_path()
    try:
        key = bytes.fromhex(path.read_text(encoding="ascii").strip())
    except FileNotFoundError:
        raise IndexUnavailable("no-key", "hash key file missing") from None
    except (OSError, ValueError):
        raise IndexUnavailable("no-key", "hash key file unreadable") from None
    if not 16 <= len(key) <= 64:
        raise IndexUnavailable("no-key", "hash key has the wrong length")
    return key


def load_index(path: Path | None = None, *, now: float | None = None) -> Index:
    path = path or state_dir() / INDEX_FILE
    try:
        blob = path.read_bytes()
    except FileNotFoundError:
        raise IndexUnavailable("no-index") from None
    except OSError as exc:
        raise IndexUnavailable("bad-index", type(exc).__name__) from None
    header, payload = _parse(blob)
    if hashlib.blake2b(payload, digest_size=16).hexdigest() != header.get("payload_blake2b"):
        raise IndexUnavailable("bad-index", "checksum mismatch")
    try:
        nd, nc, nm = (int(x) for x in header["sizes"])
        if len(payload) != 12 * nd + nc + 8 * nm:
            raise ValueError
        digests = _le_array("Q", payload[:8 * nd])
        note_ids = _le_array("I", payload[8 * nd:12 * nd])
        note_cats = payload[12 * nd:12 * nd + nc]
        marking = _le_array("Q", payload[12 * nd + nc:])
    except (KeyError, TypeError, ValueError):
        raise IndexUnavailable("bad-index", "payload does not match its header") from None
    key = load_key()
    if _key_id(key) != header.get("key_id"):
        raise IndexUnavailable("key-mismatch", "index was built with a different hash key")
    index = Index(header, key, digests, note_ids, note_cats, marking)
    age = index.age_hours(now)
    if age > MAX_AGE_HOURS or age < -1:
        raise IndexUnavailable("stale-index", age_hours=round(age, 2))
    return index


_LOCK = threading.Lock()
_CACHE: dict = {}


def _current_index() -> Index:
    """The loaded index, reloaded whenever the index or key file changes. Age is
    re-checked on every call, so a cached index still ages out."""
    path, kpath = state_dir() / INDEX_FILE, key_path()
    try:
        st = path.stat()
    except FileNotFoundError:
        raise IndexUnavailable("no-index") from None
    try:
        kst = kpath.stat()
    except FileNotFoundError:
        raise IndexUnavailable("no-key", "hash key file missing") from None
    signature = (str(path), st.st_ino, st.st_mtime_ns, st.st_size,
                 str(kpath), kst.st_ino, kst.st_mtime_ns, kst.st_size)
    with _LOCK:
        if _CACHE.get("signature") != signature:
            _CACHE.clear()
            _CACHE["index"] = load_index(path)
            _CACHE["signature"] = signature
        index = _CACHE["index"]
    age = index.age_hours()
    if age > MAX_AGE_HOURS or age < -1:
        raise IndexUnavailable("stale-index", age_hours=round(age, 2))
    return index


# ---------- checks ----------

def _audit(surface: str, verdict: Verdict) -> None:
    """One JSON line per check: counts and digests only. Never raises."""
    try:
        path = state_dir() / AUDIT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > AUDIT_MAX_BYTES:
            path.replace(path.with_suffix(".jsonl.1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(verdict.record(surface), sort_keys=True) + "\n")
    except Exception:
        pass


def check(text: str, *, surface: str) -> Verdict:
    """Screen one outbound text. Never raises: anything unexpected blocks.

    `surface` names where the text is going (e.g. "github:comment") and is
    what the audit log records instead of the text.
    """
    try:
        verdict = _current_index().check(text)
    except IndexUnavailable as exc:
        verdict = Verdict(False, exc.reason, index_age_hours=exc.age_hours)
    except Exception as exc:
        verdict = Verdict(False, f"error:{type(exc).__name__}")
    _audit(surface, verdict)
    return verdict


def enforce(text: str, *, surface: str) -> Verdict:
    """check(), raising OutboundBlocked unless the text is clear."""
    verdict = check(text, surface=surface)
    if not verdict.allowed:
        raise OutboundBlocked(verdict)
    return verdict


def readiness() -> Verdict:
    """Could the guard clear anything right now? Validates the index without
    screening text or writing the audit log."""
    try:
        index = _current_index()
    except IndexUnavailable as exc:
        return Verdict(False, exc.reason, index_age_hours=exc.age_hours)
    except Exception as exc:
        return Verdict(False, f"error:{type(exc).__name__}")
    return Verdict(True, "ready", index_age_hours=round(index.age_hours(), 2))


GitRunner = Callable[[list[str]], "tuple[bool, str]"]


def pending_push_text(run_git: GitRunner, remote: str = "origin") -> str | None:
    """Everything a push of HEAD to `remote` would publish that it does not
    have yet: commit messages, file paths and added lines of every unpushed
    commit, merge commits included (against their first parent), with
    binary, -diff and textconv attributes overridden so no content hides.
    None when git cannot say, which callers must treat as blocked."""
    unpushed = ["HEAD", "--not", f"--remotes={remote}"]
    try:
        ok, messages = run_git(["log", "--format=%B", *unpushed])
        if not ok:
            return None
        ok, patch = run_git(["log", "-p", "--text", "--no-textconv", "--no-ext-diff", "--no-renames",
                             "--diff-merges=first-parent", "--no-color", "--format=", *unpushed])
        if not ok:
            return None
    except Exception:  # e.g. undecodable output: cannot screen, so block
        return None
    parts = [messages]
    for line in patch.splitlines():
        if line.startswith("+++ "):
            parts.append(line[4:].removeprefix("b/"))
        elif line.startswith("+"):
            parts.append(line[1:])
    return "\n".join(parts)


def check_push(run_git: GitRunner, *, surface: str, extra: str = "", remote: str = "origin") -> Verdict:
    """Screen what a push would publish (plus `extra`, e.g. the PR title and
    body) as one item. Call after the commit, before `git push`."""
    text = pending_push_text(run_git, remote)
    if text is None:
        verdict = Verdict(False, "error:git")
        _audit(surface, verdict)
        return verdict
    return check(f"{text}\n{extra}" if extra else text, surface=surface)


# ---------- building ----------

@dataclass(frozen=True)
class Policy:
    marking_phrases: tuple[str, ...]
    policy_folders: tuple[str, ...]   # vault-relative, each ending in "/"


def load_policy(path: Path | None = None) -> Policy:
    path = path or policy_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PolicyError(f"private policy file missing at {path}") from None
    except (OSError, ValueError) as exc:
        raise PolicyError(f"private policy file at {path} is unreadable ({type(exc).__name__})") from None
    phrases = data.get("marking_phrases") if isinstance(data, dict) else None
    folders = data.get("policy_folders") if isinstance(data, dict) else None
    if not isinstance(phrases, list) or not phrases or not all(
            isinstance(p, str) and norm_words(p) for p in phrases):
        raise PolicyError("policy marking_phrases must be a non-empty list of phrases with words in them")
    if not isinstance(folders, list) or not all(isinstance(f, str) and f.strip("/ ") for f in folders):
        raise PolicyError("policy policy_folders must be a list (possibly empty) of vault-relative folders")
    return Policy(tuple(phrases), tuple(f.strip().strip("/") + "/" for f in folders))


def load_or_create_key(path: Path | None = None) -> bytes:
    """The hash key, created (mode 600) on first build. An existing but
    unreadable key is an error, never silently replaced."""
    path = path or key_path()
    if path.exists():
        return load_key(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another builder won the race
        return load_key(path)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(key.hex() + "\n")
    return key


def build_index(marked: list[tuple[str, str | tuple[str, ...]]], policy: Policy, key: bytes, out: Path,
                *, notes_scanned: int | None = None, extra_counts: dict | None = None) -> dict:
    """Write an index for `marked` notes, given as (gate category, text) pairs.
    The text may be a tuple of forms (e.g. cleaned and raw markdown); every
    form's shingles are indexed. Returns counts only."""
    categories = sorted({cat for cat, _ in marked} | {"policy-folder"})
    if len(categories) > 256:
        raise BuildError("too many gate categories")
    cat_index = {cat: i for i, cat in enumerate(categories)}
    owner: dict[int, int] = {}
    note_cats = bytearray()
    for note_id, (cat, forms) in enumerate(marked):
        note_cats.append(cat_index[cat])
        for form in ((forms,) if isinstance(forms, str) else forms):
            for d in _shingles(norm_words(form), key):
                owner.setdefault(d, note_id)
    digests = sorted(owner)
    phrases = [" ".join(norm_words(p)) for p in policy.marking_phrases]
    marking = array("Q", sorted({_digest(key, p) for p in phrases}))
    built_at = time.time()
    counts = {
        "notes_scanned": notes_scanned if notes_scanned is not None else len(marked),
        "marked_notes": len(marked),
        "marked_by_category": dict(sorted(Counter(cat for cat, _ in marked).items())),
        "shingles": len(digests),
        "marking_phrases": len(marking),
        **(extra_counts or {}),
    }
    header = {
        "format": FORMAT, "n": N, "threshold": THRESHOLD,
        "built_at": built_at,
        "built_at_utc": datetime.fromtimestamp(built_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "key_id": _key_id(key),
        "categories": categories,
        "marking_lengths": sorted({len(p.split()) for p in phrases}),
        "counts": counts,
    }
    _write_index(out, header, array("Q", digests), array("I", (owner[d] for d in digests)),
                 bytes(note_cats), marking)
    return counts


def _import_gate():
    """Gemma GRC's corpus gate (vault_ingest.py): the one definition of a marked note."""
    where = gate_dir()
    if not (where / "vault_ingest.py").is_file():
        raise BuildError(f"corpus gate not found at {where}/vault_ingest.py")
    if str(where) not in sys.path:
        sys.path.append(str(where))  # append, so it never shadows Neural Bridge modules
    import vault_ingest  # noqa: E402
    for name in ("gate_note", "read_note", "clean"):
        if not callable(getattr(vault_ingest, name, None)):
            raise BuildError(f"corpus gate at {where} has no {name}()")
    return vault_ingest


def _fold(path: str) -> str:
    """Case- and Unicode-form-insensitive path, for policy-folder matching."""
    return unicodedata.normalize("NFC", path).casefold()


def build_from_vault(*, vault: Path | None = None, policy: Policy | None = None,
                     out: Path | None = None, gate=None) -> dict:
    """Walk the vault as leak_scan.py does and write the index. `gate` must
    offer gate_note(), read_note() and clean(); the default is Gemma GRC's.

    Each marked note is indexed from both its cleaned text (what a reader
    sees) and its raw markdown (what a tool that reads the file sees), so
    links and embeds cannot break a quoted run apart. A policy folder that
    matches no note fails the build, so a typo cannot unprotect a folder.
    """
    vault = Path(vault or vault_path())
    if not vault.is_dir():
        raise BuildError(f"vault not found at {vault}")
    policy = policy or load_policy()
    gate = gate or _import_gate()
    key = load_or_create_key()
    folders = [_fold(f) for f in policy.policy_folders]
    folder_notes = [0] * len(folders)
    marked: list[tuple[str, tuple[str, str]]] = []
    scanned = 0
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        scanned += 1
        in_folders = [i for i, f in enumerate(folders) if _fold(rel.as_posix()).startswith(f)]
        for i in in_folders:
            folder_notes[i] += 1
        raw = gate.read_note(path)
        ok, reason, _ = gate.gate_note(raw)
        if not ok:
            marked.append((str(reason).split(":")[0], (gate.clean(raw), raw)))
        elif in_folders:
            marked.append(("policy-folder", (gate.clean(raw), raw)))
    if not scanned:
        raise BuildError(f"no notes found under {vault}")
    empty = [str(i + 1) for i, n in enumerate(folder_notes) if n == 0]
    if empty:  # by position, not name: the names are private
        raise BuildError(f"policy folder(s) {', '.join(empty)} of {len(folders)} matched no notes; "
                         f"check the policy file")
    if not marked:
        raise BuildError("no marked notes found; refusing to build an index that would clear everything")
    return build_index(marked, policy, key, out or state_dir() / INDEX_FILE, notes_scanned=scanned,
                       extra_counts={"policy_folder_notes": folder_notes})


def refresh(timeout: int = BUILD_TIMEOUT) -> tuple[bool, str]:
    """Rebuild in a child process, which keeps the vault walk (and Gemma GRC's
    modules) out of long-running callers. Returns (ok, one line)."""
    try:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "build"],
                              capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"build timed out after {timeout}s"
    except OSError as exc:
        return False, f"build could not start ({type(exc).__name__})"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        return False, (err[-1] if err else f"build exited {proc.returncode}")[:300]
    out = (proc.stdout or "").strip().splitlines()
    return True, (out[-1] if out else "built")[:300]


def ensure_fresh(max_age_hours: float = REFRESH_AFTER_HOURS) -> tuple[bool, str]:
    """Rebuild unless a usable index at most `max_age_hours` old is in place."""
    ready = readiness()
    if ready.allowed and ready.index_age_hours is not None and ready.index_age_hours <= max_age_hours:
        return True, f"index ready ({ready.index_age_hours:.1f}h old)"
    return refresh()


def status() -> dict:
    """Index health for operators: age and counts, never text."""
    out: dict = {"state_dir": str(state_dir()), "policy": str(policy_path())}
    try:
        index = _current_index()
    except IndexUnavailable as exc:
        out.update(ready=False, reason=exc.reason, detail=str(exc), age_hours=exc.age_hours)
        return out
    out.update(ready=True, built_at=index.header.get("built_at_utc"),
               age_hours=round(index.age_hours(), 2), counts=index.header.get("counts"))
    return out


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="rebuild the index from the vault")
    sub.add_parser("status", help="index age and counts")
    chk = sub.add_parser("check", help="screen stdin; prints counts only; exit 1 when blocked")
    chk.add_argument("--surface", default="cli:check")
    args = ap.parse_args(argv)

    if args.cmd == "build":
        try:
            c = build_from_vault()
        except (GuardError, IndexUnavailable) as exc:
            print(f"outbound guard build failed: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # the type only: a message could quote a note
            print(f"outbound guard build failed: {type(exc).__name__}", file=sys.stderr)
            return 1
        print(f"outbound guard index built: {c['marked_notes']} marked notes, {c['shingles']} shingles, "
              f"{c['marking_phrases']} marking phrases, {c['notes_scanned']} notes scanned")
        return 0
    if args.cmd == "status":
        s = status()
        print(json.dumps(s, indent=2, sort_keys=True))
        return 0 if s["ready"] else 1
    verdict = check(sys.stdin.read(), surface=args.surface)
    print(verdict.describe())
    return 0 if verdict.allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
