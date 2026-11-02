"""Fixed read-only project checks. No project code or command is executed."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from collections import Counter
from collections.abc import Callable
from pathlib import Path

MAX_ENTRIES = 4096
MAX_DEPTH = 12
MAX_FILES = 2048
MAX_BYTES = 32 * 1024 * 1024
MAX_METADATA = 64 * 1024
EXTENSIONS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".toml", ".md", ".txt",
    ".rst", ".html", ".css", ".rs", ".go",
})
SKIP_DIRS = frozenset({
    "node_modules", "vendor", "dist", "build", "coverage", "target", "venv",
    "__pycache__",
})
METADATA = frozenset({"package.json", "pyproject.toml"})


class CheckError(ValueError):
    """Fixed codes only; never expose a project filename or file content."""


def _invalid_json_constant(_value: str) -> None:
    raise CheckError("checkFailed")


def open_project_root(value: str) -> int:
    """Pin a canonical root through no-follow directory descriptors."""
    root = Path(value)
    home = Path.home()
    if (
        not value or len(value.encode("utf-8")) > 1024
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or not root.is_absolute() or str(root) != value or ".." in root.parts
        or len(root.parts) < 4 or root == home or root in home.parents
        or value in {"/private/var/tmp", "/private/var/folders", "/private/var/run"}
    ):
        raise CheckError("invalidRoot")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in root.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise CheckError("invalidRoot") from None


def _metadata(fd: int, name: str, expected: os.stat_result) -> None:
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = os.fstat(file_fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise CheckError("checkFailed")
        if info.st_size > MAX_METADATA:
            raise CheckError("checkLimit")
        with os.fdopen(file_fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_METADATA + 1)
        if len(data) > MAX_METADATA:
            raise CheckError("checkLimit")
        try:
            text = data.decode("utf-8")
            parsed = (
                json.loads(text, parse_constant=_invalid_json_constant)
                if name == "package.json" else tomllib.loads(text)
            )
        except (ValueError, RecursionError):
            raise CheckError("checkFailed") from None
        if not isinstance(parsed, dict):
            raise CheckError("checkFailed")
    finally:
        os.close(file_fd)


def scan_project(
    root_fd: int,
    *,
    checkpoint: Callable[[], None],
    progress: Callable[[int], None],
) -> str:
    entries = files = size = skipped = metadata = 0
    readme = False
    extensions: Counter[str] = Counter()
    device = os.fstat(root_fd).st_dev
    last_progress = 0

    def walk(fd: int, depth: int) -> None:
        nonlocal entries, files, size, skipped, metadata, readme, last_progress
        with os.scandir(fd) as iterator:
            for entry in iterator:
                checkpoint()
                if entries == MAX_ENTRIES:
                    raise CheckError("checkLimit")
                entries += 1
                if entries % 64 == 0:
                    progress(entries)
                    last_progress = entries
                name = entry.name
                if name.startswith("."):
                    skipped += 1
                    continue
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != device or stat.S_ISLNK(info.st_mode):
                    skipped += 1
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if name.lower() in SKIP_DIRS:
                        skipped += 1
                        continue
                    if depth == MAX_DEPTH:
                        raise CheckError("checkLimit")
                    child = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd,
                    )
                    try:
                        pinned = os.fstat(child)
                        if (pinned.st_dev, pinned.st_ino) != (info.st_dev, info.st_ino):
                            raise CheckError("checkFailed")
                        walk(child, depth + 1)
                    finally:
                        os.close(child)
                    continue
                ext = Path(name).suffix.lower()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or ext not in EXTENSIONS:
                    skipped += 1
                    continue
                if files == MAX_FILES or size + info.st_size > MAX_BYTES:
                    raise CheckError("checkLimit")
                files += 1
                size += info.st_size
                extensions[ext] += 1
                if depth == 0:
                    readme = readme or name.lower() in {"readme.md", "readme.txt", "readme.rst"}
                    if name in METADATA:
                        _metadata(fd, name, info)
                        metadata += 1

    walk(root_fd, 0)
    checkpoint()
    if entries != last_progress:
        progress(entries)
    findings = []
    if not readme:
        findings.append("missingReadme")
    if not metadata:
        findings.append("missingMetadata")
    types = ", ".join(f"{ext}: {count}" for ext, count in sorted(extensions.items())) or "none"
    return (
        "Built-in project check finished.\n"
        f"Visited entries: {entries}\nSelected files: {files}\n"
        f"Selected bytes: {size}\nSkipped entries: {skipped}\n"
        f"Validated metadata files: {metadata}\nFile types: {types}\n"
        f"Findings: {', '.join(findings) or 'none'}\n"
        "No project commands or model were run."
    )
