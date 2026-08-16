"""Tests for scripts/env_file.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import env_file  # noqa: E402


# ---------- parsing ----------

def test_plain_pairs():
    assert env_file.parse_env_text("A=1\nB=two\n") == {"A": "1", "B": "two"}


def test_strips_export_prefix():
    assert env_file.parse_env_text("export TOKEN=abc") == {"TOKEN": "abc"}


def test_strips_matching_quotes():
    parsed = env_file.parse_env_text('A="hello world"\nB=\'single\'\n')
    assert parsed == {"A": "hello world", "B": "single"}


def test_quoted_value_keeps_internal_hash():
    """The vault path is quoted in Andy's real file; a '#' inside quotes is data."""
    parsed = env_file.parse_env_text('P="/Users/a/Doc # ument"')
    assert parsed["P"] == "/Users/a/Doc # ument"


def test_unquoted_trailing_comment_is_stripped():
    assert env_file.parse_env_text("A=1 # why")["A"] == "1"


def test_unquoted_hash_without_space_is_kept():
    """Fragments and generated tokens contain '#' with no leading space."""
    assert env_file.parse_env_text("URL=http://h/p#frag")["URL"] == "http://h/p#frag"


def test_skips_comments_and_blanks():
    assert env_file.parse_env_text("# note\n\n  \nA=1\n") == {"A": "1"}


def test_skips_malformed_lines_without_raising():
    """A 400-line secrets file with one bad line must not take down a daemon."""
    parsed = env_file.parse_env_text("garbage-no-equals\n9BAD=x\nA B=y\nGOOD=z\n")
    assert parsed == {"GOOD": "z"}


def test_value_containing_equals_is_preserved():
    assert env_file.parse_env_text("K=a=b=c")["K"] == "a=b=c"


def test_empty_value_allowed():
    assert env_file.parse_env_text("K=")["K"] == ""


# ---------- loading ----------

def test_existing_environment_wins(tmp_path, monkeypatch):
    """The launchd plists set variables inline. Adding the same key to the file
    must not change what an already-configured scheduled job sees."""
    f = tmp_path / ".env"
    f.write_text("LUNA_TEST_KEY=from_file\n")
    monkeypatch.setenv("LUNA_TEST_KEY", "from_plist")
    applied = env_file.load_env_file(f)
    assert os.environ["LUNA_TEST_KEY"] == "from_plist"
    assert "LUNA_TEST_KEY" not in applied


def test_override_true_stomps_environment(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("LUNA_TEST_KEY=from_file\n")
    monkeypatch.setenv("LUNA_TEST_KEY", "from_plist")
    env_file.load_env_file(f, override=True)
    assert os.environ["LUNA_TEST_KEY"] == "from_file"


def test_sets_when_absent(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("LUNA_TEST_ABSENT=value\n")
    monkeypatch.delenv("LUNA_TEST_ABSENT", raising=False)
    assert env_file.load_env_file(f) == ["LUNA_TEST_ABSENT"]
    assert os.environ["LUNA_TEST_ABSENT"] == "value"


def test_missing_file_is_not_an_error(tmp_path):
    assert env_file.load_env_file(tmp_path / "nope.env") == []


def test_directory_instead_of_file_is_not_an_error(tmp_path):
    assert env_file.load_env_file(tmp_path) == []


def test_returns_key_names_only_never_values(tmp_path, monkeypatch):
    """These files hold tokens. The return value is what gets logged, so it
    must not carry secrets."""
    f = tmp_path / ".env"
    f.write_text("LUNA_TEST_SECRET=hunter2\n")
    monkeypatch.delenv("LUNA_TEST_SECRET", raising=False)
    applied = env_file.load_env_file(f)
    assert applied == ["LUNA_TEST_SECRET"]
    assert "hunter2" not in "".join(applied)


def test_first_path_wins_across_defaults(tmp_path, monkeypatch):
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    first.write_text("LUNA_TEST_ORDER=first\n")
    second.write_text("LUNA_TEST_ORDER=second\n")
    monkeypatch.delenv("LUNA_TEST_ORDER", raising=False)
    monkeypatch.setattr(env_file, "DEFAULT_ENV_PATHS", (first, second))
    env_file.load_default_env()
    assert os.environ["LUNA_TEST_ORDER"] == "first"


def test_load_default_env_tolerates_all_paths_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(env_file, "DEFAULT_ENV_PATHS",
                        (tmp_path / "x.env", tmp_path / "y.env"))
    assert env_file.load_default_env() == []


@pytest.fixture(autouse=True)
def _clean_test_keys():
    yield
    for key in [k for k in os.environ if k.startswith("LUNA_TEST_")]:
        os.environ.pop(key, None)
