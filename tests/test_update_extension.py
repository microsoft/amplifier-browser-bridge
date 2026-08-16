"""Tests for update_extension.py -- the verify-or-guide `browser_update_extension` tool.

No real HubClient/websocket/browser is exercised here -- a fake `_FakeClient`
(same duck-typing pattern as test_archive.py's `FakeArchiveClient`) answers
`list_devices()`/`command()` from a scripted sequence, and every assertion
inspects the returned summary dict. `setup.stage_extension` is monkeypatched
to a fake that never touches the real filesystem or this repo's real
extension source.

Covers CONTRIBUTING.md's evidence-based testing convention for this feature:
already-current no-op, reload-then-verify success, reload-then-verify
FAILURE producing guided instructions, device-never-returns-after-reload
failing loud, the reload-unsupported bootstrap limit, a not-currently-live
device, an unknown device_id, and a restage failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.extension_integrity import ExtensionIntegrityError
from amplifier_browser_bridge.setup import ExtensionSourceNotFoundError
from amplifier_browser_bridge.update_extension import run_update_extension

_DEVICE_ID = "d1"
_HUB_URL = "ws://100.64.1.2:8900/agent"


def _device(
    *,
    commands: list[str] | None = "UNSET",  # type: ignore[assignment]
    tier: str = "live",
    connected: bool = True,
    connected_at: str = "2026-08-15T00:00:00+00:00",
    in_sync: bool = False,
    build_stamp: str | None = "UNSET",  # type: ignore[assignment]
    build_current: bool | None = None,
) -> dict[str, Any]:
    if commands == "UNSET":
        commands = ["snapshot", "click"] if not in_sync else ["snapshot", "click", "reload"]
    # Defaults keep every PRE-EXISTING test (written before the build-stamp axis
    # existed) passing unchanged: unless a test explicitly says otherwise, the
    # build axis moves in lockstep with the command axis. Tests that need to
    # prove the two axes are checked INDEPENDENTLY pass `build_current`
    # explicitly (see the "command-complete but stale build" tests below).
    if build_current is None:
        build_current = in_sync
    if build_stamp == "UNSET":
        build_stamp = "current-stamp" if build_current else "stale-stamp"
    return {
        "device_id": _DEVICE_ID,
        "label": "edge-macos",
        "platform": "MacIntel",
        "commands": commands,
        "manifest_version": "0.5.0",
        "build_stamp": build_stamp,
        "connected": connected,
        "tier": tier,
        "connected_at": connected_at,
        "skew": {"known": commands is not None, "in_sync": in_sync, "device_behind": [], "hub_behind": []},
        "build_freshness": {
            "known": build_stamp is not None,
            "current": build_current,
            "device_stamp": build_stamp,
            "hub_stamp": "current-stamp",
        },
    }


class _FakeClient:
    """Duck-typed HubClient -- `devices_sequence` is consumed one item per
    `list_devices()` call (the last entry repeats once exhausted, so a poll
    loop that calls more times than scripted just keeps seeing the final
    state -- used by the reconnect-timeout test)."""

    def __init__(
        self,
        devices_sequence: list[list[dict[str, Any]]],
        command_responses: dict[str, dict[str, Any]] | None = None,
        url: str = _HUB_URL,
    ) -> None:
        self.url = url
        self._devices_sequence = devices_sequence
        self._call_index = 0
        self._command_responses = command_responses or {}
        self.commands_sent: list[tuple[str, str, dict[str, Any]]] = []

    async def list_devices(self) -> list[dict[str, Any]]:
        idx = min(self._call_index, len(self._devices_sequence) - 1)
        self._call_index += 1
        return self._devices_sequence[idx]

    async def command(self, target: Target, command: str, args: dict[str, Any]) -> dict[str, Any]:
        self.commands_sent.append((target.device_id, command, dict(args)))
        return self._command_responses.get(command, {"ok": True, "result": {}})


def _fake_stage_extension(
    monkeypatch: pytest.MonkeyPatch, *, staged_dir: Path, raises: Exception | None = None
) -> None:
    def _fake(dest: Any = None, source: Any = None) -> Path:
        if raises is not None:
            raise raises
        return staged_dir

    monkeypatch.setattr("amplifier_browser_bridge.update_extension.stage_extension", _fake)


def _assert_stage_extension_never_called(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(dest: Any = None, source: Any = None) -> Path:
        raise AssertionError("stage_extension should not have been called")

    monkeypatch.setattr("amplifier_browser_bridge.update_extension.stage_extension", _boom)


# ---------------------------------------------------------------------------


def test_unknown_device_id_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_stage_extension_never_called(monkeypatch)
    client = _FakeClient(devices_sequence=[[]])

    result = _run(client, "nope")
    assert result["ok"] is False
    assert "unknown device_id" in result["error"]


def test_already_current_is_a_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Non-negotiable: if already current on BOTH axes, do nothing and say
    so -- no restage, no reload, no churn."""
    _assert_stage_extension_never_called(monkeypatch)
    before = _device(in_sync=True)
    client = _FakeClient(devices_sequence=[[before]])

    result = _run(client, _DEVICE_ID)
    assert result["ok"] is True
    assert result["already_current"] is True
    assert result["updated"] is False
    assert result["build_stamp"] == "current-stamp"
    assert client.commands_sent == []  # no reload sent


def test_command_complete_but_stale_build_is_not_reported_already_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact gap proven live against a real browser: a device that is
    command-complete (skew.in_sync) but running a stale build (a bug/UI/
    security fix that touched zero commands -- e.g. this repo's own commits
    6175ce4/cc140c5) must NOT be reported `already_current`. A non-live tier
    isolates the property under test: if the already-current shortcut had
    (incorrectly) fired, this would return `ok: true` instead of the
    `device_not_live` failure below -- it must not have fired."""
    _assert_stage_extension_never_called(monkeypatch)
    before = _device(tier="intermittent", connected=False, in_sync=True, build_current=False)
    client = _FakeClient(devices_sequence=[[before]])

    result = _run(client, _DEVICE_ID)
    assert result["ok"] is False
    assert result["reason"] == "device_not_live"
    assert "already_current" not in result


def test_command_incomplete_but_current_build_is_not_reported_already_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction of the same requirement: a device running the
    hub's exact current build but still missing a command (skew.in_sync is
    False) must also not be reported already_current."""
    _assert_stage_extension_never_called(monkeypatch)
    before = _device(tier="intermittent", connected=False, in_sync=False, build_current=True)
    client = _FakeClient(devices_sequence=[[before]])

    result = _run(client, _DEVICE_ID)
    assert result["ok"] is False
    assert result["reason"] == "device_not_live"
    assert "already_current" not in result


def test_reload_then_verify_success_via_build_stamp_only_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact incident this closes: a bug/UI fix that adds/removes zero
    commands must still be detected as a genuine, verified update via the
    build stamp changing -- the command set is IDENTICAL before and after."""
    _fake_stage_extension(monkeypatch, staged_dir=tmp_path)
    before = _device(
        commands=["snapshot", "click", "reload"],
        connected_at="2026-08-15T00:00:00+00:00",
        in_sync=True,
        build_stamp="stale-stamp",
        build_current=False,
    )
    after = _device(
        commands=["snapshot", "click", "reload"],  # UNCHANGED
        connected_at="2026-08-15T00:00:05+00:00",  # NEW connection
        in_sync=True,
        build_stamp="fresh-stamp",  # CHANGED -- this is the only signal that moved
        build_current=True,
    )
    client = _FakeClient(
        devices_sequence=[[before], [after]],
        command_responses={"reload": {"ok": True, "result": {"reloading": True}}},
    )

    result = _run(client, _DEVICE_ID, poll_interval_s=0.01)
    assert result["ok"] is True
    assert result["updated"] is True
    assert result["before_build_stamp"] == "stale-stamp"
    assert result["after_build_stamp"] == "fresh-stamp"
    assert result["now_build_current"] is True
    assert "build stamp changed" in result["message"]


def test_reload_then_verify_failure_when_neither_commands_nor_build_stamp_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Broadened failure mode: the automatic path is unverifiable only when
    NEITHER axis moved -- proving the restage genuinely never reached this
    device's real extension files."""
    _fake_stage_extension(monkeypatch, staged_dir=tmp_path)
    before = _device(
        commands=["snapshot", "click"],
        connected_at="2026-08-15T00:00:00+00:00",
        in_sync=False,
        build_stamp="same-stamp",
        build_current=False,
    )
    after = _device(
        commands=["snapshot", "click"],  # UNCHANGED
        connected_at="2026-08-15T00:00:05+00:00",  # genuinely reconnected
        in_sync=False,
        build_stamp="same-stamp",  # ALSO UNCHANGED
        build_current=False,
    )
    client = _FakeClient(
        devices_sequence=[[before], [after]],
        command_responses={"reload": {"ok": True, "result": {"reloading": True}}},
    )

    result = _run(client, _DEVICE_ID, poll_interval_s=0.01)
    assert result["ok"] is False
    assert result["reason"] == "no_verified_change"
    assert "UNCHANGED" in result["error"]
    assert result["before_build_stamp"] == "same-stamp"


def test_device_not_live_fails_loud_without_restaging(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_stage_extension_never_called(monkeypatch)
    before = _device(tier="intermittent", connected=False, in_sync=False)
    client = _FakeClient(devices_sequence=[[before]])

    result = _run(client, _DEVICE_ID)
    assert result["ok"] is False
    assert result["reason"] == "device_not_live"
    assert client.commands_sent == []


def test_restage_failure_produces_guided_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(dest: Any = None, source: Any = None) -> Path:
        raise ExtensionSourceNotFoundError("no extension/ source found")

    monkeypatch.setattr("amplifier_browser_bridge.update_extension.stage_extension", _boom)
    before = _device(in_sync=False)
    client = _FakeClient(devices_sequence=[[before]])

    result = _run(client, _DEVICE_ID)
    assert result["ok"] is False
    assert result["reason"] == "restage_failed"
    assert result["guided"]["download_url"] == "http://100.64.1.2:8900/setup/extension.zip"
    assert client.commands_sent == []


def test_reload_unsupported_is_the_bootstrap_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An extension too old to understand `reload` at all can never reload
    itself -- must be reported as the guided path, not retried."""
    _fake_stage_extension(monkeypatch, staged_dir=tmp_path)
    before = _device(in_sync=False)
    client = _FakeClient(
        devices_sequence=[[before]],
        command_responses={"reload": {"ok": False, "error": "unsupported command: reload"}},
    )

    result = _run(client, _DEVICE_ID)
    assert result["ok"] is False
    assert result["reason"] == "reload_unsupported"
    assert "bootstrap limit" in result["error"]
    assert result["guided"]["download_url"].startswith("http://100.64.1.2:8900")


def test_reload_then_verify_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tier 1: reload succeeds, device reconnects (new connected_at), and its
    command set genuinely changed -- this IS the proof, not an assumption."""
    _fake_stage_extension(monkeypatch, staged_dir=tmp_path)
    before = _device(commands=None, connected_at="2026-08-15T00:00:00+00:00", in_sync=False)  # pre-handshake
    after = _device(
        commands=["snapshot", "click", "reload"],
        connected_at="2026-08-15T00:00:05+00:00",  # NEW connection
        in_sync=True,
    )
    client = _FakeClient(
        devices_sequence=[[before], [after]],
        command_responses={"reload": {"ok": True, "result": {"reloading": True}}},
    )

    result = _run(client, _DEVICE_ID, poll_interval_s=0.01)
    assert result["ok"] is True
    assert result["updated"] is True
    assert result["before_commands_count"] is None
    assert result["after_commands_count"] == 3
    assert result["now_in_sync"] is True
    assert ("d1", "reload", {}) in client.commands_sent


def test_reload_then_verify_failure_produces_guided_instructions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The core design insight: reload succeeds and the device reconnects,
    but its command set is UNCHANGED -- this hub's restage did not reach
    wherever the browser's real extension files live. Must be reported
    plainly, never as success."""
    _fake_stage_extension(monkeypatch, staged_dir=tmp_path)
    before = _device(commands=["snapshot", "click"], connected_at="2026-08-15T00:00:00+00:00", in_sync=False)
    after = _device(
        commands=["snapshot", "click"],  # UNCHANGED
        connected_at="2026-08-15T00:00:05+00:00",  # genuinely reconnected
        in_sync=False,
    )
    client = _FakeClient(
        devices_sequence=[[before], [after]],
        command_responses={"reload": {"ok": True, "result": {"reloading": True}}},
    )

    result = _run(client, _DEVICE_ID, poll_interval_s=0.01)
    assert result["ok"] is False
    assert result["reason"] == "no_verified_change"
    assert "UNCHANGED" in result["error"]
    assert result["guided"]["download_url"] == "http://100.64.1.2:8900/setup/extension.zip"
    assert "edge://extensions" in result["guided"]["instructions"]


def test_device_never_reconnects_fails_loud(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Must never silently "verify" against a stale pre-reload connection --
    a real, bounded timeout, and an honest failure when it's exceeded."""
    _fake_stage_extension(monkeypatch, staged_dir=tmp_path)
    before = _device(commands=["snapshot", "click"], connected_at="2026-08-15T00:00:00+00:00", in_sync=False)
    # Every subsequent poll sees the SAME (stale) connected_at -- device never came back.
    client = _FakeClient(
        devices_sequence=[[before]],
        command_responses={"reload": {"ok": True, "result": {"reloading": True}}},
    )

    result = _run(client, _DEVICE_ID, reconnect_timeout_s=0.05, poll_interval_s=0.01)
    assert result["ok"] is False
    assert result["reason"] == "reconnect_timeout"
    assert "did not reconnect" in result["error"]
    assert "guided" not in result  # distinct failure mode -- not (yet) a guided-path recommendation


def test_download_url_derivation_uses_wss_as_https(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(dest: Any = None, source: Any = None) -> Path:
        raise ExtensionSourceNotFoundError("boom")

    monkeypatch.setattr("amplifier_browser_bridge.update_extension.stage_extension", _boom)
    before = _device(in_sync=False)
    client = _FakeClient(devices_sequence=[[before]], url="wss://100.64.1.2:8900/agent")

    result = _run(client, _DEVICE_ID)
    assert result["guided"]["download_url"] == "https://100.64.1.2:8900/setup/extension.zip"


def test_restage_failure_via_integrity_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(dest: Any = None, source: Any = None) -> Path:
        raise ExtensionIntegrityError("staged directory missing a file")

    monkeypatch.setattr("amplifier_browser_bridge.update_extension.stage_extension", _boom)
    before = _device(in_sync=False)
    client = _FakeClient(devices_sequence=[[before]])

    result = _run(client, _DEVICE_ID)
    assert result["ok"] is False
    assert result["reason"] == "restage_failed"


# ---------------------------------------------------------------------------


def _run(client: _FakeClient, device_id: str, **kwargs: Any) -> dict[str, Any]:
    import asyncio

    return asyncio.run(run_update_extension(client, device_id, **kwargs))  # type: ignore[arg-type]


def test_loopback_download_url_warns_it_is_unreachable_remotely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guided path exists BECAUSE the browser is on another machine, and the
    hub's own default bind is 127.0.0.1 -- so handing over a loopback URL with no
    warning is the likely case, not the exotic one, and is a silent dead end."""

    def _boom(dest: Any = None, source: Any = None) -> Path:
        raise ExtensionSourceNotFoundError("boom")

    monkeypatch.setattr("amplifier_browser_bridge.update_extension.stage_extension", _boom)
    client = _FakeClient(devices_sequence=[[_device(in_sync=False)]], url="ws://127.0.0.1:8900/agent")

    guided = _run(client, _DEVICE_ID)["guided"]
    assert guided["download_url"] == "http://127.0.0.1:8900/setup/extension.zip"
    assert "LOOPBACK" in guided["warning"]
    assert "tailscale" in guided["warning"].lower()


def test_routable_download_url_carries_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(dest: Any = None, source: Any = None) -> Path:
        raise ExtensionSourceNotFoundError("boom")

    monkeypatch.setattr("amplifier_browser_bridge.update_extension.stage_extension", _boom)
    client = _FakeClient(devices_sequence=[[_device(in_sync=False)]], url="ws://100.64.1.2:8900/agent")

    guided = _run(client, _DEVICE_ID)["guided"]
    assert guided["download_url"] == "http://100.64.1.2:8900/setup/extension.zip"
    assert "warning" not in guided
