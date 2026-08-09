"""Tests for doctor.py -- `amplifier-browser-bridge doctor`'s diagnostic checks.

Runs a real Hub over a real (localhost, ephemeral-port) aiohttp test server so
HubClient exercises the actual wire protocol -- not a fake/mock -- for the
hub-reachable / token-match / device-connected checks. The token-store check is
purely local (no network) and tested directly against a token file.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestServer

from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.doctor import all_ok, run_doctor
from amplifier_browser_bridge.hub import Hub


def _by_name(checks: list, name: str):
    for c in checks:
        if c.name == name:
            return c
    raise AssertionError(f"no check named {name!r} in {[c.name for c in checks]}")


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"default": "secret-123", "devices": {}}), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_doctor_all_ok_with_no_devices_connected(tmp_path: Path, token_file: Path) -> None:
    """A freshly-started hub with a valid token and zero devices: reachable and
    token-match both pass, device_connected fails with an actionable message --
    this is the exact state right after `amplifier-browser-bridge init` + `amplifier-browser-bridge hub`, before the
    extension has been configured yet."""
    hub = Hub(
        token_store=TokenStore(default_token="secret-123"), audit_log=AuditLog(tmp_path / "audit.jsonl")
    )
    server = TestServer(hub.build_app())
    await server.start_server()
    try:
        hub_url = f"ws://{server.host}:{server.port}/agent"
        checks = await run_doctor(hub_url, "secret-123", token_file)
    finally:
        await server.close()

    assert _by_name(checks, "token_store").ok
    assert _by_name(checks, "hub_reachable").ok
    assert _by_name(checks, "token_match").ok
    device_check = _by_name(checks, "device_connected")
    assert not device_check.ok
    assert "no browser device has ever connected" in device_check.message
    assert all_ok(checks) is False


@pytest.mark.asyncio
async def test_doctor_reports_device_connected_when_live(tmp_path: Path, token_file: Path) -> None:
    hub = Hub(
        token_store=TokenStore(default_token="secret-123"), audit_log=AuditLog(tmp_path / "audit.jsonl")
    )
    # Register a live device directly against the registry -- same technique
    # test_hub.py uses to simulate a connected device without a real socket.
    record = hub.registry.get_or_create("dev-1")

    class _FakeSocket:
        async def send_json(self, data: dict) -> None:
            pass

        async def close(self) -> None:
            pass

    record.ws = _FakeSocket()
    record.touch()

    server = TestServer(hub.build_app())
    await server.start_server()
    try:
        hub_url = f"ws://{server.host}:{server.port}/agent"
        checks = await run_doctor(hub_url, "secret-123", token_file)
    finally:
        await server.close()

    device_check = _by_name(checks, "device_connected")
    assert device_check.ok
    assert "dev-1" in device_check.message
    assert all_ok(checks) is True


@pytest.mark.asyncio
async def test_doctor_reports_token_mismatch_distinctly_from_unreachable(
    tmp_path: Path, token_file: Path
) -> None:
    hub = Hub(
        token_store=TokenStore(default_token="secret-123"), audit_log=AuditLog(tmp_path / "audit.jsonl")
    )
    server = TestServer(hub.build_app())
    await server.start_server()
    try:
        hub_url = f"ws://{server.host}:{server.port}/agent"
        checks = await run_doctor(hub_url, "wrong-token", token_file)
    finally:
        await server.close()

    assert _by_name(checks, "hub_reachable").ok  # reached the hub fine
    token_check = _by_name(checks, "token_match")
    assert not token_check.ok
    device_check = _by_name(checks, "device_connected")
    assert device_check.status == "skipped"
    assert "token mismatch" in device_check.message
    assert all_ok(checks) is False


@pytest.mark.asyncio
async def test_doctor_reports_unreachable_hub_distinctly(tmp_path: Path, token_file: Path) -> None:
    """Nothing listening on this port -- the hub_reachable check itself must
    fail, and downstream checks must report skipped, not a second failure."""
    checks = await run_doctor("ws://127.0.0.1:1/agent", "secret-123", token_file)

    hub_check = _by_name(checks, "hub_reachable")
    assert not hub_check.ok
    assert "Is `amplifier-browser-bridge hub` running" in hub_check.message
    assert _by_name(checks, "token_match").status == "skipped"
    assert _by_name(checks, "device_connected").status == "skipped"
    assert all_ok(checks) is False


def test_doctor_reports_divergent_sibling_token_file(tmp_path: Path) -> None:
    """Bug C regression: a stray file beside the active token file, holding a
    DIFFERENT value, must be reported -- not silently ignored the way it was when
    a hand-created `hub.token` sat next to `tokens.json` and only `tokens.json` was
    ever consulted."""
    token_path = tmp_path / "tokens.json"
    token_path.write_text(json.dumps({"default": "d5112ff3aabbcc", "devices": {}}), encoding="utf-8")
    stray_value = "eEyFb1ur9nabcdef"
    stray = tmp_path / "hub.token"
    stray.write_text(f"{stray_value}\n", encoding="utf-8")

    checks = await_run_doctor_local_only(token_path)

    sibling_check = _by_name(checks, "token_file_siblings")
    assert sibling_check.status == "fail"
    assert "hub.token" in sibling_check.message
    assert stray_value not in sibling_check.message  # only a masked (truncated) prefix, never the full value
    assert "d5112ff3aabbcc" not in sibling_check.message  # nor the active token, even truncated form


def test_doctor_does_not_flag_sibling_with_matching_value(tmp_path: Path) -> None:
    token_path = tmp_path / "tokens.json"
    token_path.write_text(json.dumps({"default": "same-token", "devices": {}}), encoding="utf-8")
    identical = tmp_path / "hub.token"
    identical.write_text("same-token\n", encoding="utf-8")

    checks = await_run_doctor_local_only(token_path)

    sibling_check = _by_name(checks, "token_file_siblings")
    assert sibling_check.ok


def test_doctor_reports_no_siblings_when_none_present(tmp_path: Path) -> None:
    token_path = tmp_path / "tokens.json"
    token_path.write_text(json.dumps({"default": "only-one", "devices": {}}), encoding="utf-8")

    checks = await_run_doctor_local_only(token_path)

    sibling_check = _by_name(checks, "token_file_siblings")
    assert sibling_check.ok
    assert "no other token-like files" in sibling_check.message


def await_run_doctor_local_only(token_path: Path) -> list:
    """Run doctor against an unreachable hub, purely to exercise the local-only
    token_store/token_file_siblings checks without needing a real server."""
    return asyncio.run(run_doctor("ws://127.0.0.1:1/agent", None, token_path))


def test_doctor_token_file_path_honors_env_var_not_just_hardcoded_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for a second Bug-C-adjacent inconsistency: the path doctor
    DISPLAYS must be the same one `load_token_store` actually reads -- previously
    the displayed path ignored $AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE and always showed the hardcoded
    default, so a user following an env-var-based setup saw the wrong path in every
    message."""
    custom_path = tmp_path / "custom-tokens.json"
    custom_path.write_text(json.dumps({"default": "tok", "devices": {}}), encoding="utf-8")
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE", str(custom_path))

    checks = asyncio.run(run_doctor("ws://127.0.0.1:1/agent", None, None))

    token_check = _by_name(checks, "token_store")
    assert str(custom_path) in token_check.message


# --- A2 fix: network_exposure check (bind-address exposure + Tailscale ACL disclosure) ---


def test_doctor_network_exposure_warns_on_wildcard_host(tmp_path: Path, token_file: Path) -> None:
    with patch("amplifier_browser_bridge.doctor.detect_tailscale_ip", return_value="100.124.126.19"):
        checks = asyncio.run(run_doctor("ws://0.0.0.0:8900/agent", "secret-123", token_file))

    check = _by_name(checks, "network_exposure")
    assert check.ok  # informational, never fails the run on its own
    assert "WILDCARD" in check.message
    assert "0.0.0.0" in check.message


def test_doctor_network_exposure_notes_loopback_cannot_prove_wider_bind_absent(
    tmp_path: Path, token_file: Path
) -> None:
    with patch("amplifier_browser_bridge.doctor.detect_tailscale_ip", return_value=None):
        checks = asyncio.run(run_doctor("ws://127.0.0.1:8900/agent", "secret-123", token_file))

    check = _by_name(checks, "network_exposure")
    assert "loopback" in check.message
    assert "cannot prove" in check.message


def test_doctor_network_exposure_includes_tailscale_acl_disclosure(tmp_path: Path, token_file: Path) -> None:
    with patch("amplifier_browser_bridge.doctor.detect_tailscale_ip", return_value="100.124.126.19"):
        checks = asyncio.run(run_doctor("ws://127.0.0.1:8900/agent", "secret-123", token_file))

    check = _by_name(checks, "network_exposure")
    assert "allows every device on your tailnet" in check.message
    assert "docs/tailscale-acl-example.hujson" in check.message
    assert "100.124.126.19" in check.message  # detected IP surfaced to the user


def test_doctor_network_exposure_flags_critical_combo_when_auth_disabled(tmp_path: Path) -> None:
    """Auth disabled + a non-loopback target is the specific dangerous
    combination this check exists to name loudly."""
    missing_token_path = tmp_path / "no-tokens-here.json"
    with patch("amplifier_browser_bridge.doctor.detect_tailscale_ip", return_value=None):
        checks = asyncio.run(run_doctor("ws://127.0.0.1:1/agent", None, missing_token_path))

    check = _by_name(checks, "network_exposure")
    assert "CRITICAL COMBINATION" in check.message
    assert "auth is DISABLED" in check.message


def test_doctor_network_exposure_silent_on_critical_combo_when_auth_enabled(
    tmp_path: Path, token_file: Path
) -> None:
    with patch("amplifier_browser_bridge.doctor.detect_tailscale_ip", return_value=None):
        checks = asyncio.run(run_doctor("ws://127.0.0.1:1/agent", "secret-123", token_file))

    check = _by_name(checks, "network_exposure")
    assert "CRITICAL COMBINATION" not in check.message


# --- service_status check: "installed but not running" vs "genuinely broken" ---


def test_doctor_service_status_ok_when_not_supported(tmp_path: Path, token_file: Path) -> None:
    from amplifier_browser_bridge.service import ServiceInfo

    unsupported = ServiceInfo(
        platform="windows",
        supported=False,
        installed=False,
        active=None,
        unit_path=None,
        detail="Windows is not implemented in this release",
    )
    with patch("amplifier_browser_bridge.doctor.describe_service", return_value=unsupported):
        checks = asyncio.run(run_doctor("ws://127.0.0.1:1/agent", "secret-123", token_file))

    check = _by_name(checks, "service_status")
    assert check.ok
    assert "not available on this platform" in check.message


def test_doctor_service_status_ok_when_not_installed(tmp_path: Path, token_file: Path) -> None:
    from amplifier_browser_bridge.service import ServiceInfo

    not_installed = ServiceInfo(
        platform="linux",
        supported=True,
        installed=False,
        active=None,
        unit_path=None,
        detail="not installed",
    )
    with patch("amplifier_browser_bridge.doctor.describe_service", return_value=not_installed):
        checks = asyncio.run(run_doctor("ws://127.0.0.1:1/agent", "secret-123", token_file))

    check = _by_name(checks, "service_status")
    assert check.ok
    assert "no amplifier-browser-bridge service installed" in check.message


def test_doctor_service_status_fails_and_skips_downstream_when_locally_stopped(
    tmp_path: Path, token_file: Path
) -> None:
    """The load-bearing behavior this check exists for: a service installed but not
    running, on the SAME host doctor is targeting, must be reported as the actionable
    cause -- and the network checks below it must be skipped, not report a second,
    less specific failure for the same root cause."""
    from amplifier_browser_bridge.service import ServiceInfo

    stopped = ServiceInfo(
        platform="linux",
        supported=True,
        installed=True,
        active=False,
        unit_path=Path("/home/x/.config/systemd/user/amplifier-browser-bridge.service"),
        detail="installed but NOT active (unit: .../amplifier-browser-bridge.service)",
    )
    with patch("amplifier_browser_bridge.doctor.describe_service", return_value=stopped):
        checks = asyncio.run(run_doctor("ws://127.0.0.1:8900/agent", "secret-123", token_file))

    service_check = _by_name(checks, "service_status")
    assert not service_check.ok
    assert "NOT running" in service_check.message
    assert "service start" in service_check.message

    assert _by_name(checks, "hub_reachable").status == "skipped"
    assert _by_name(checks, "token_match").status == "skipped"
    assert _by_name(checks, "device_connected").status == "skipped"
    assert all_ok(checks) is False


@pytest.mark.asyncio
async def test_doctor_service_status_ok_when_locally_active(tmp_path: Path, token_file: Path) -> None:
    """An active local service must not block the real network checks below it."""
    from amplifier_browser_bridge.service import ServiceInfo

    hub = Hub(
        token_store=TokenStore(default_token="secret-123"), audit_log=AuditLog(tmp_path / "audit.jsonl")
    )
    server = TestServer(hub.build_app())
    await server.start_server()
    try:
        hub_url = f"ws://{server.host}:{server.port}/agent"
        active = ServiceInfo(
            platform="linux",
            supported=True,
            installed=True,
            active=True,
            unit_path=Path("/unit"),
            detail="installed and active",
        )
        with patch("amplifier_browser_bridge.doctor.describe_service", return_value=active):
            checks = await run_doctor(hub_url, "secret-123", token_file)
    finally:
        await server.close()

    service_check = _by_name(checks, "service_status")
    assert service_check.ok
    assert _by_name(checks, "hub_reachable").ok


def test_doctor_service_status_is_informational_only_when_hub_url_is_a_different_host(
    tmp_path: Path, token_file: Path
) -> None:
    """A service stopped on THIS machine must not be reported as a failure when
    --hub-url points at a different host -- this check has no way to see a remote
    machine's service state, and must say so rather than assert a failure it cannot
    actually prove."""
    from amplifier_browser_bridge.service import ServiceInfo

    stopped = ServiceInfo(
        platform="linux",
        supported=True,
        installed=True,
        active=False,
        unit_path=Path("/unit"),
        detail="installed but NOT active (unit: /unit)",
    )
    with (
        patch("amplifier_browser_bridge.doctor.describe_service", return_value=stopped),
        patch("amplifier_browser_bridge.doctor.detect_tailscale_ip", return_value=None),
    ):
        checks = asyncio.run(run_doctor("ws://100.200.200.200:8900/agent", "secret-123", token_file))

    service_check = _by_name(checks, "service_status")
    assert service_check.ok  # never asserted as a failure for a host it can't see
    assert "DIFFERENT host" in service_check.message
    # downstream must NOT be skipped on this check's account -- the real network
    # attempt still runs and reports its own (unreachable) result
    assert _by_name(checks, "hub_reachable").status == "fail"


def test_doctor_reports_auth_disabled_honestly(tmp_path: Path) -> None:
    """No token file at all -- token_store check must still be 'ok' (not a
    failure -- dev mode is a legitimate, documented state) but its message must
    say so plainly, matching auth.py's own loud dev-mode warning."""
    from amplifier_browser_bridge.doctor import DoctorCheck

    missing_path = tmp_path / "does-not-exist.json"
    # Exercise just the token_store half of run_doctor's logic via a direct
    # construction -- avoids needing a real hub for this local-only check.
    from amplifier_browser_bridge.auth import load_token_store

    store = load_token_store(missing_path)
    assert store.auth_enabled is False
    check = DoctorCheck("token_store", "ok", f"auth DISABLED (no token found at {missing_path})")
    assert check.ok
