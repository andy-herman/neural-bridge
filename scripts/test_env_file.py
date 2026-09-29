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


@pytest.mark.parametrize("value", ["$HOME/private/snapshot.json", "~/private/snapshot.json"])
def test_values_are_not_interpolated(value):
    assert env_file.parse_env_text(f"K={value}")["K"] == value


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


@pytest.mark.parametrize("override", [False, True])
def test_allowlist_excludes_unselected_keys_even_when_overriding(tmp_path, monkeypatch, override):
    f = tmp_path / "selected.env"
    f.write_text("LUNA_TEST_SELECTED=file\nLUNA_TEST_SECRET=file\nLUNA_TEST_PROVIDER=file\n")
    monkeypatch.setenv("LUNA_TEST_SELECTED", "inherited")
    monkeypatch.setenv("LUNA_TEST_SECRET", "unchanged")
    monkeypatch.delenv("LUNA_TEST_PROVIDER", raising=False)

    applied = env_file.load_env_file(f, keys={"LUNA_TEST_SELECTED"}, override=override)

    assert applied == (["LUNA_TEST_SELECTED"] if override else [])
    assert os.environ["LUNA_TEST_SELECTED"] == ("file" if override else "inherited")
    assert os.environ["LUNA_TEST_SECRET"] == "unchanged"
    assert "LUNA_TEST_PROVIDER" not in os.environ


@pytest.mark.parametrize("override", [False, True])
def test_empty_allowlist_loads_nothing(tmp_path, monkeypatch, override):
    f = tmp_path / "empty-allowlist.env"
    f.write_text("LUNA_TEST_PRESENT=file\nLUNA_TEST_ABSENT=file\n")
    monkeypatch.setenv("LUNA_TEST_PRESENT", "unchanged")
    monkeypatch.delenv("LUNA_TEST_ABSENT", raising=False)
    monkeypatch.setattr(env_file, "DEFAULT_ENV_PATHS", (f,))

    assert env_file.load_env_file(f, keys=set(), override=override) == []
    assert env_file.load_default_env(keys=set(), override=override) == []
    assert os.environ["LUNA_TEST_PRESENT"] == "unchanged"
    assert "LUNA_TEST_ABSENT" not in os.environ


@pytest.mark.parametrize("kwargs", [{}, {"keys": None}], ids=["omitted", "none"])
@pytest.mark.parametrize("override", [False, True])
def test_default_allowlist_preserves_all_key_behavior(tmp_path, monkeypatch, kwargs, override):
    first, second = tmp_path / "shared.env", tmp_path / "local.env"
    first.write_text("LUNA_TEST_ONE=one\nLUNA_TEST_TWO=two\n")
    second.write_text("LUNA_TEST_THREE=three\nLUNA_TEST_TWO=later\n")
    for key in ("LUNA_TEST_ONE", "LUNA_TEST_TWO", "LUNA_TEST_THREE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(env_file, "DEFAULT_ENV_PATHS", (first, second))

    assert env_file.load_env_file(first, override=override, **kwargs) == [
        "LUNA_TEST_ONE", "LUNA_TEST_TWO",
    ]
    applied = env_file.load_default_env(override=override, **kwargs)

    assert applied == (["LUNA_TEST_ONE", "LUNA_TEST_TWO", "LUNA_TEST_THREE", "LUNA_TEST_TWO"]
                       if override else ["LUNA_TEST_THREE"])
    assert os.environ["LUNA_TEST_ONE"] == "one"
    assert os.environ["LUNA_TEST_TWO"] == ("later" if override else "two")
    assert os.environ["LUNA_TEST_THREE"] == "three"


@pytest.mark.parametrize("inherited", [None, "", "from_process"])
@pytest.mark.parametrize("shared", [None, "", "first"])
@pytest.mark.parametrize("override", [False, True])
def test_allowlisted_default_precedence(tmp_path, monkeypatch, inherited, shared, override):
    first, second = tmp_path / "shared.env", tmp_path / "local.env"
    first.write_text("" if shared is None else f"LUNA_TEST_ORDER={shared}\n")
    second.write_text("LUNA_TEST_ORDER=second\nLUNA_TEST_PROVIDER=file\n")
    monkeypatch.delenv("LUNA_TEST_PROVIDER", raising=False)
    if inherited is None:
        monkeypatch.delenv("LUNA_TEST_ORDER", raising=False)
    else:
        monkeypatch.setenv("LUNA_TEST_ORDER", inherited)
    monkeypatch.setattr(env_file, "DEFAULT_ENV_PATHS", (first, second))

    applied = env_file.load_default_env(keys={"LUNA_TEST_ORDER"}, override=override)

    if override:
        assert os.environ["LUNA_TEST_ORDER"] == "second"
        assert applied == ["LUNA_TEST_ORDER"] * (1 if shared is None else 2)
    elif inherited is not None:
        assert os.environ["LUNA_TEST_ORDER"] == inherited
        assert applied == []
    else:
        assert os.environ["LUNA_TEST_ORDER"] == ("second" if shared is None else shared)
        assert applied == ["LUNA_TEST_ORDER"]
    assert "LUNA_TEST_PROVIDER" not in os.environ


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
