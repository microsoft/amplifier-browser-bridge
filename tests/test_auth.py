"""Tests for auth.py's sibling-token-file detection (Bug C).

The concrete failure mode under test: `amplifier-browser-bridge init` reported "Reusing existing hub
token" -- true of the token file it actually reads (`tokens.json`) -- while an
unrelated file sitting right beside it (e.g. a hand-created `hub.token`) held a
DIFFERENT value and was never consulted by anything. A user who pasted the wrong
file's contents into the extension's options page would only discover the mismatch
through a confusing auth failure. `find_sibling_token_files`/`extract_token_value`
are the detection primitives `amplifier-browser-bridge doctor` and `amplifier-browser-bridge init` both use to catch this
proactively instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from amplifier_browser_bridge.auth import (
    extract_token_value,
    find_sibling_token_files,
    mask_token,
    resolve_token_file,
)


def test_find_sibling_token_files_finds_name_containing_token(tmp_path: Path) -> None:
    active = tmp_path / "tokens.json"
    active.write_text(json.dumps({"default": "a", "devices": {}}), encoding="utf-8")
    stray = tmp_path / "hub.token"
    stray.write_text("some-other-value\n", encoding="utf-8")
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("irrelevant", encoding="utf-8")

    siblings = find_sibling_token_files(active)

    assert siblings == [stray]


def test_find_sibling_token_files_excludes_the_active_file_itself(tmp_path: Path) -> None:
    active = tmp_path / "tokens.json"
    active.write_text(json.dumps({"default": "a", "devices": {}}), encoding="utf-8")

    assert find_sibling_token_files(active) == []


def test_find_sibling_token_files_returns_empty_when_directory_missing(tmp_path: Path) -> None:
    missing_dir_file = tmp_path / "does-not-exist" / "tokens.json"
    assert find_sibling_token_files(missing_dir_file) == []


def test_extract_token_value_from_json_token_file(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"default": "abc123", "devices": {}}), encoding="utf-8")
    assert extract_token_value(path) == "abc123"


def test_extract_token_value_from_bare_text_file(tmp_path: Path) -> None:
    """A hand-created `hub.token` is plausibly just the raw token, no JSON wrapper."""
    path = tmp_path / "hub.token"
    path.write_text("eEyFb1ur9nabcdef\n", encoding="utf-8")
    assert extract_token_value(path) == "eEyFb1ur9nabcdef"


def test_extract_token_value_returns_none_for_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "hub.token"
    path.write_text("", encoding="utf-8")
    assert extract_token_value(path) is None


def test_extract_token_value_returns_none_for_garbage_json(tmp_path: Path) -> None:
    path = tmp_path / "hub.token"
    path.write_text(json.dumps({"unrelated": "shape"}), encoding="utf-8")
    assert extract_token_value(path) is None


def test_mask_token_truncates_long_tokens() -> None:
    masked = mask_token("d5112ff3aabbccddeeff00112233")
    assert masked == "d5112ff3..."
    assert "aabbccdd" not in masked  # never leak past the prefix


def test_mask_token_handles_short_tokens() -> None:
    assert mask_token("short") == "***"


def test_resolve_token_file_honors_env_var(tmp_path: Path, monkeypatch) -> None:
    """The core regression for the doctor.py display bug: the path shown to a user
    must come from the SAME resolution order `load_token_store` uses, including
    $AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE -- not just an explicit --token-file or the hardcoded default."""
    custom = tmp_path / "custom-tokens.json"
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE", str(custom))
    assert resolve_token_file(None) == custom


def test_resolve_token_file_explicit_path_wins_over_env(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / "env-tokens.json"
    explicit_path = tmp_path / "explicit-tokens.json"
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE", str(env_path))
    assert resolve_token_file(explicit_path) == explicit_path
