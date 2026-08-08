"""Tests for android_bake.py -- resolving the hub URL + token baked into the
Android CRX at pack time by scripts/package-android.sh.

The load-bearing properties under test:
  1. Read-only w.r.t. the hub's own token file -- this module must never write
     there (that's ensure_token_file/`amplifier-browser-bridge init`'s job).
  2. Fail loud (BakeConfigError), never silently fabricate a value, when the hub
     URL or token cannot be safely resolved.
  3. The written bundled_config.json has exactly the shape background.js's
     fetchBundledConfig()/resolveBundledConfigAdoption() expect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplifier_browser_bridge.android_bake import (
    DEFAULT_BAKE_PORT,
    BakeConfigError,
    build_bundled_config,
    cli_main,
    resolve_bake_hub_url,
    resolve_bake_token,
    write_bundled_config,
)

# --- resolve_bake_hub_url ------------------------------------------------------


def test_resolve_bake_hub_url_explicit_url_wins_over_everything() -> None:
    assert (
        resolve_bake_hub_url("ws://10.0.0.5:9999/device", "ignored-host", 1234) == "ws://10.0.0.5:9999/device"
    )


def test_resolve_bake_hub_url_rejects_non_ws_scheme() -> None:
    with pytest.raises(BakeConfigError, match="ws://"):
        resolve_bake_hub_url("http://10.0.0.5:8900/device", None, DEFAULT_BAKE_PORT)


def test_resolve_bake_hub_url_composes_explicit_host_and_port() -> None:
    assert resolve_bake_hub_url(None, "100.1.2.3", 8900) == "ws://100.1.2.3:8900/device"


def test_resolve_bake_hub_url_falls_back_to_tailscale_autodetect(monkeypatch: pytest.MonkeyPatch) -> None:
    import amplifier_browser_bridge.android_bake as bake_mod

    monkeypatch.setattr(bake_mod, "detect_tailscale_ip", lambda: "100.124.126.19")
    assert resolve_bake_hub_url(None, None, 8900) == "ws://100.124.126.19:8900/device"


def test_resolve_bake_hub_url_fails_loud_with_no_loopback_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike `amplifier-browser-bridge init`, there is deliberately NO 127.0.0.1
    fallback here -- a baked config resolving to loopback would tell the PHONE to
    connect to itself, silently and permanently wrong."""
    import amplifier_browser_bridge.android_bake as bake_mod

    monkeypatch.setattr(bake_mod, "detect_tailscale_ip", lambda: None)
    with pytest.raises(BakeConfigError, match="Tailscale IP"):
        resolve_bake_hub_url(None, None, 8900)


# --- resolve_bake_token ---------------------------------------------------------


def test_resolve_bake_token_reads_from_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"default": "file-token", "devices": {}}), encoding="utf-8")

    token, auth_disabled = resolve_bake_token(allow_no_token=False, token_file=token_file)

    assert token == "file-token"
    assert auth_disabled is False


def test_resolve_bake_token_uses_env_var_when_no_token_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN", "env-token")
    missing_file = tmp_path / "does-not-exist.json"

    token, auth_disabled = resolve_bake_token(allow_no_token=False, token_file=missing_file)

    assert token == "env-token"
    assert auth_disabled is False


def test_resolve_bake_token_file_default_wins_over_env_var_when_both_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This mirrors `load_token_store`'s own (pre-existing) resolution order -- the
    token FILE's "default" key, when present, wins over the env var. `resolve_bake_token`
    is a thin wrapper and must agree with the hub's own resolution exactly, since a
    baked config that disagreed with what the hub itself would accept would be a
    silent, very confusing mismatch."""
    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"default": "file-token", "devices": {}}), encoding="utf-8")
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN", "env-token")

    token, auth_disabled = resolve_bake_token(allow_no_token=False, token_file=token_file)

    assert token == "file-token"
    assert auth_disabled is False


def test_resolve_bake_token_missing_fails_loud_by_default(tmp_path: Path) -> None:
    missing_file = tmp_path / "does-not-exist.json"
    with pytest.raises(BakeConfigError, match="No hub token found"):
        resolve_bake_token(allow_no_token=False, token_file=missing_file)


def test_resolve_bake_token_missing_with_allow_no_token_returns_empty_and_disabled(tmp_path: Path) -> None:
    missing_file = tmp_path / "does-not-exist.json"
    token, auth_disabled = resolve_bake_token(allow_no_token=True, token_file=missing_file)
    assert token == ""
    assert auth_disabled is True


def test_resolve_bake_token_never_writes_the_token_file(tmp_path: Path) -> None:
    """The core read-only regression test: calling this against a nonexistent token
    file must never create one -- that would be exactly the kind of silent write to
    the hub's own config this module's docstring promises never to do."""
    missing_file = tmp_path / "does-not-exist.json"
    resolve_bake_token(allow_no_token=True, token_file=missing_file)
    assert not missing_file.exists()

    existing_file = tmp_path / "tokens.json"
    existing_file.write_text(json.dumps({"default": "real-token", "devices": {}}), encoding="utf-8")
    before = existing_file.read_text(encoding="utf-8")
    resolve_bake_token(allow_no_token=False, token_file=existing_file)
    after = existing_file.read_text(encoding="utf-8")
    assert before == after


# --- build_bundled_config / write_bundled_config --------------------------------


def test_build_bundled_config_end_to_end(tmp_path: Path) -> None:
    real_token = "abc123def456abc123def456abc123d"  # 32 hex-shaped chars, like generate_token()'s output
    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"default": real_token, "devices": {}}), encoding="utf-8")

    config = build_bundled_config(hub_host="100.1.2.3", hub_port=8900, token_file=token_file)

    assert config.hub_url == "ws://100.1.2.3:8900/device"
    assert config.hub_token == real_token
    assert config.auth_disabled is False
    assert config.token_masked.startswith(real_token[:8])
    # Never leaks the FULL raw token into the masked display form.
    assert config.token_masked != real_token
    # generated_at is a real ISO-8601 UTC timestamp, not a placeholder.
    assert config.generated_at.endswith("Z")


def test_write_bundled_config_writes_expected_json_shape_and_permissions(tmp_path: Path) -> None:
    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"default": "abc123", "devices": {}}), encoding="utf-8")
    config = build_bundled_config(hub_host="100.1.2.3", hub_port=8900, token_file=token_file)

    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    out_path = write_bundled_config(stage_dir, config)

    assert out_path == stage_dir / "bundled_config.json"
    data = json.loads(out_path.read_text(encoding="utf-8"))
    # Exactly the shape extension/bundled_config.mjs's resolveBundledConfigAdoption
    # (via background.js's fetchBundledConfig) expects.
    assert set(data.keys()) == {"hubUrl", "hubToken", "generatedAt"}
    assert data["hubUrl"] == "ws://100.1.2.3:8900/device"
    assert data["hubToken"] == "abc123"

    mode = out_path.stat().st_mode & 0o777
    assert mode == 0o600, (
        f"bundled_config.json must be chmod 600 (carries a live credential); got {oct(mode)}"
    )


def test_write_bundled_config_with_allow_no_token_writes_empty_token(tmp_path: Path) -> None:
    missing_file = tmp_path / "does-not-exist.json"
    config = build_bundled_config(hub_host="100.1.2.3", allow_no_token=True, token_file=missing_file)
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    out_path = write_bundled_config(stage_dir, config)

    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["hubToken"] == ""


# --- cli_main --------------------------------------------------------------------


def test_cli_main_success_writes_file_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"default": "abc123", "devices": {}}), encoding="utf-8")
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()

    rc = cli_main(
        [
            "--stage-dir",
            str(stage_dir),
            "--hub-host",
            "100.1.2.3",
            "--token-file",
            str(token_file),
        ]
    )

    assert rc == 0
    assert (stage_dir / "bundled_config.json").is_file()
    captured = capsys.readouterr()
    assert "SECURITY NOTICE" in captured.err
    # Never prints the raw token to the build log.
    assert "abc123" not in captured.err


def test_cli_main_refuses_loudly_when_no_token_and_not_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_file = tmp_path / "does-not-exist.json"
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()

    rc = cli_main(
        [
            "--stage-dir",
            str(stage_dir),
            "--hub-host",
            "100.1.2.3",
            "--token-file",
            str(missing_file),
        ]
    )

    assert rc == 1
    assert not (stage_dir / "bundled_config.json").exists()
    captured = capsys.readouterr()
    assert "BUILD REFUSED" in captured.err


def test_cli_main_allow_no_token_warns_and_still_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_file = tmp_path / "does-not-exist.json"
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()

    rc = cli_main(
        [
            "--stage-dir",
            str(stage_dir),
            "--hub-host",
            "100.1.2.3",
            "--token-file",
            str(missing_file),
            "--allow-no-token",
        ]
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert "auth DISABLED" in captured.err
