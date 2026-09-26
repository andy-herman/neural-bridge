"""Synthetic outbound-guard state for tests.

The repo is public, so every "marked" note, marking phrase and folder name here
is invented. Nothing in this module reads the real vault, the real policy file,
the real index or the real public clones: SyntheticGuard points the guard's
environment variables at a throwaway directory, empties the public-repo list,
and aims the vault and corpus-gate locations at paths that do not exist, so an
accidental rebuild fails instead of reading real notes.

Tokens in the synthetic notes carry no inner punctuation (no hyphens or
apostrophes), so N whitespace tokens are exactly N normalized words and an
excerpt of N words yields N - 11 shingles.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from scripts import outbound_guard as og

MARKED_NOTE = """Project Halyard review, week nine.

This note follows the standard weekly review template: status, decisions, risks, next checkpoint, and owners for each open action item.

The routing pilot in the Aldermoor depot cut average dwell time from forty one minutes to twenty six, but only after the night crew stopped double scanning pallets at the inbound dock. Finance wants the savings booked against the third quarter, which the pilot owners resist because the cold chain lanes were excluded from the measurement window.

Our recommendation is to extend the pilot to the Brackenfield and Tolland depots in the spring, keep the cold chain lanes out until the sensor retrofit lands, and publish nothing about the dwell numbers until the carrier contracts are renegotiated.

Open risks: the scanner firmware vendor has not confirmed support past March, two supervisors have asked to rotate off nights, and the union steward wants the double scan rule written down before anyone is disciplined for skipping it. Next checkpoint is the steering call on the fourteenth.
"""

UNMARKED_NOTE = """Garden log, late September.

This note follows the standard weekly review template: status, decisions, risks, next checkpoint, and owners for each open action item.

The tomatoes finally split after the heavy rain, so the last of them went into a sauce with the basil that survived the aphids. Next year the beds along the fence need more afternoon shade and a drip line on a timer.
"""

# The sentence both notes share. The guard does not discount text that also
# appears in unmarked notes, so quoting it is blocked like any marked passage.
BOILERPLATE = ("This note follows the standard weekly review template: status, decisions, risks, "
               "next checkpoint, and owners for each open action item.")

FILLER = (
    "A good sourdough starter wants a warm corner of the kitchen, flour with some whole grain in it, "
    "and a regular feeding schedule that you can actually keep. Most failures come from impatience "
    "rather than from bad flour or bad water. Give the starter a week of steady feedings before judging "
    "it, and write down what you did each day so you can repeat what worked. When the loaf finally "
    "rises, the crumb will tell you whether the dough was underproofed or overproofed far better than "
    "any recipe can."
)

MARKING_PHRASE = "Zephyrine Internal Only"
MARKING_TAG = "halyard-restricted"

# Words that appear only in the synthetic marked note or marking phrases. None
# may ever show up in an index file, an audit line or a verdict description.
SECRET_WORDS = ("halyard", "aldermoor", "brackenfield", "tolland", "zephyrine", "dwell")


def marked_excerpt(n_words: int, start_word: str = "routing") -> str:
    """n_words consecutive words of MARKED_NOTE, starting at start_word."""
    tokens = MARKED_NOTE.split()
    i = tokens.index(start_word)
    if i + n_words > len(tokens):
        raise ValueError("excerpt runs past the end of the synthetic note")
    return " ".join(tokens[i:i + n_words])


def planted(excerpt: str) -> str:
    """The excerpt dropped into the middle of unrelated filler."""
    cut = len(FILLER) // 2
    cut = FILLER.index(" ", cut)
    return f"{FILLER[:cut]} {excerpt} {FILLER[cut:]}"


class SyntheticGuard:
    """Point the outbound guard at throwaway state built from synthetic notes.

    install() writes a policy, a hash key and an index under a temp directory
    and sets the guard's environment variables; remove() restores them. Works
    as a context manager or from setUpModule/tearDownModule.
    """

    def __init__(self, marked=None, marking_phrases=None, policy_folders=()):
        self.marked = list(marked) if marked is not None else [("tag", MARKED_NOTE)]
        self.marking_phrases = list(marking_phrases or [MARKING_PHRASE, MARKING_TAG])
        self.policy_folders = list(policy_folders)
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._saved: dict[str, str | None] = {}

    def install(self) -> "SyntheticGuard":
        self._tmp = tempfile.TemporaryDirectory(prefix="nb-outbound-guard-")
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "state"
        self.policy_file = self.root / "config" / "outbound-guard.json"
        self.policy_file.parent.mkdir(parents=True)
        self.policy_file.write_text(json.dumps({
            "marking_phrases": self.marking_phrases,
            "policy_folders": self.policy_folders,
        }), encoding="utf-8")
        env = {
            og.ENV_DIR: str(self.state_dir),
            og.ENV_POLICY: str(self.policy_file),
            og.ENV_VAULT: str(self.root / "no-vault-here"),
            og.ENV_GATE: str(self.root / "no-gate-here"),
            og.ENV_PUBLIC: "",  # no public repos: never read the real clones
        }
        for name, value in env.items():
            self._saved[name] = os.environ.get(name)
            os.environ[name] = value
        self.rebuild()
        return self

    def rebuild(self) -> dict:
        return og.build_index(self.marked, og.load_policy(), og.load_or_create_key(), self.index_path)

    @property
    def index_path(self) -> Path:
        return self.state_dir / og.INDEX_FILE

    @property
    def audit_path(self) -> Path:
        return self.state_dir / og.AUDIT_FILE

    @property
    def key_path(self) -> Path:
        return og.key_path()

    def audit_records(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines() if line]

    def remove(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._saved.clear()
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def __enter__(self) -> "SyntheticGuard":
        return self.install()

    def __exit__(self, *exc) -> None:
        self.remove()
