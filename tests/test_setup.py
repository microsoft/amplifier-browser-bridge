"""Tests for setup.py -- the `amplifier-browser-bridge init` helpers.

The load-bearing property under test: re-running these functions (simulating an
`amplifier-browser-bridge init` re-run after `git pull`) must NEVER destroy an existing token or change the
staging directory's path -- that's the exact "update clobbers your config" bug this
project shipped with (extension/config.js).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import amplifier_browser_bridge.setup as setup_mod
from amplifier_browser_bridge.extension_integrity import ExtensionIntegrityError
from amplifier_browser_bridge.setup import (
    ExtensionSourceNotFoundError,
    ensure_token_file,
    find_extension_source,
    generate_token,
    stage_extension,
)


def test_generate_token_is_high_entropy_and_unique() -> None:
    a = generate_token()
    b = generate_token()
    assert a != b
    assert len(a) == 32  # 16 bytes hex-encoded
    int(a, 16)  # must be valid hex


def test_ensure_token_file_creates_new_token(tmp_path: Path) -> None:
    token_path = tmp_path / "tokens.json"
    result = ensure_token_file(token_path)

    assert result.created_new is True
    assert result.token_file == token_path
    assert token_path.is_file()
    data = json.loads(token_path.read_text(encoding="utf-8"))
    assert data["default"] == result.token
    assert data["devices"] == {}


def test_ensure_token_file_is_idempotent(tmp_path: Path) -> None:
    """The core regression test: calling this twice (simulating `amplifier-browser-bridge init` run once,
    then re-run after a later `git pull`) must return the SAME token both times."""
    token_path = tmp_path / "tokens.json"
    first = ensure_token_file(token_path)
    second = ensure_token_file(token_path)

    assert second.created_new is False
    assert second.token == first.token


def test_ensure_token_file_force_regenerates(tmp_path: Path) -> None:
    token_path = tmp_path / "tokens.json"
    first = ensure_token_file(token_path)
    second = ensure_token_file(token_path, force=True)

    assert second.created_new is True
    assert second.token != first.token


def test_ensure_token_file_preserves_existing_devices_map(tmp_path: Path) -> None:
    """A hub operator may have hand-added per-device token overrides -- re-running
    `amplifier-browser-bridge init` without --force must not touch the file at all (not even to reformat
    it), so those overrides survive untouched."""
    token_path = tmp_path / "tokens.json"
    token_path.write_text(
        json.dumps({"default": "existing-token", "devices": {"dev-1": "special-token"}}), encoding="utf-8"
    )

    result = ensure_token_file(token_path)

    assert result.created_new is False
    assert result.token == "existing-token"
    data = json.loads(token_path.read_text(encoding="utf-8"))
    assert data["devices"] == {"dev-1": "special-token"}


def test_find_extension_source_resolves_to_real_repo_extension_dir() -> None:
    """This repo IS the editable-install case setup.py documents -- prove the
    inference actually finds this repo's own extension/ directory."""
    source = find_extension_source()
    assert (source / "manifest.json").is_file()
    assert (source / "background.js").is_file()


def test_find_extension_source_honors_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_EXTENSION_SRC", str(tmp_path))
    assert find_extension_source() == tmp_path


def test_find_extension_source_env_override_missing_manifest_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_EXTENSION_SRC", str(tmp_path))
    with pytest.raises(ExtensionSourceNotFoundError):
        find_extension_source()


def test_stage_extension_copies_runtime_files(tmp_path: Path) -> None:
    dest = tmp_path / "staged"
    result = stage_extension(dest=dest)

    assert result == dest
    assert (dest / "manifest.json").is_file()
    assert (dest / "background.js").is_file()
    assert (dest / "options.html").is_file()
    assert (dest / "options.js").is_file()
    assert (dest / "config_validate.mjs").is_file()
    # Test files are dev-only and must NOT be staged into a runtime install.
    assert not (dest / "config_validate.test.mjs").exists()


def test_stage_extension_is_safe_to_rerun_and_preserves_dest_path(tmp_path: Path) -> None:
    """The core regression test for the update story: re-staging into the SAME dest
    (simulating a `git pull` + `amplifier-browser-bridge init` re-run) must succeed and leave the directory
    at the same path -- proving file updates never require a new extension identity."""
    dest = tmp_path / "staged"
    first = stage_extension(dest=dest)

    # Simulate local drift (a file a previous run wrote that a real chrome install
    # would also have -- e.g. nothing this function manages) -- re-run must not choke.
    second = stage_extension(dest=dest)

    assert first == second == dest
    assert (dest / "manifest.json").is_file()


def test_stage_extension_missing_source_file_fails_loud(tmp_path: Path) -> None:
    incomplete_source = tmp_path / "incomplete_source"
    incomplete_source.mkdir()
    (incomplete_source / "manifest.json").write_text("{}", encoding="utf-8")
    # background.js deliberately missing

    with pytest.raises(ExtensionSourceNotFoundError):
        stage_extension(dest=tmp_path / "dest", source=incomplete_source)


def test_stage_extension_refuses_when_a_whitelisted_file_imports_an_unwhitelisted_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression test for the actual shipped bug (commit 87ce68d): background.js
    (real source, unmodified) imports `./effects_collector.mjs` at module top level.
    Simulate the exact historical mistake by removing ONLY that name from
    `_EXTENSION_FILES` -- everything else about the real source tree is untouched --
    and prove `stage_extension()` now refuses instead of silently handing back a
    staged directory that would kill the entire MV3 service worker on next load.

    This reproduces the bug at the level it actually shipped: a whitelist omission,
    not a hand-built broken fixture.
    """
    assert "effects_collector.mjs" in setup_mod._EXTENSION_FILES, (
        "test assumption stale: effects_collector.mjs is no longer part of the real "
        "staging whitelist -- update this test to reflect whatever file background.js "
        "imports today."
    )
    broken_whitelist = tuple(f for f in setup_mod._EXTENSION_FILES if f != "effects_collector.mjs")
    monkeypatch.setattr(setup_mod, "_EXTENSION_FILES", broken_whitelist)

    with pytest.raises(ExtensionIntegrityError, match="effects_collector.mjs") as exc_info:
        stage_extension(dest=tmp_path / "staged")

    assert exc_info.value.args
    # Fail loud, not "warn and continue": this must be a raised exception (non-zero
    # exit at the CLI layer via cli.py's ClickException translation), never a printed
    # warning with a zero return.
