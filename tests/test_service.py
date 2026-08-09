"""Tests for service.py -- `amplifier-browser-bridge service ...`'s systemd/launchd management.

All subprocess.run calls are mocked -- these tests verify the SHAPE of what would be
written/run, not real systemd/launchd behavior. Real end-to-end proof (install, start,
reachable, stop, uninstall against the actual systemd on this machine) is a manual
verification step documented in the PR, not part of this automated suite -- mutating
this developer's own real systemd --user state from an automated test run is exactly
the kind of side effect this project's tests avoid elsewhere too.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import amplifier_browser_bridge.service as svc
from amplifier_browser_bridge.service import (
    ServiceUnsupportedError,
    describe_service,
    service_install,
    service_logs,
    service_restart,
    service_start,
    service_status,
    service_stop,
    service_uninstall,
)


def _capturing_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# Platform detection / binary resolution
# ---------------------------------------------------------------------------


def test_is_darwin_and_is_windows_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert svc._is_darwin() is True
    assert svc._is_windows() is False

    monkeypatch.setattr(sys, "platform", "win32")
    assert svc._is_darwin() is False
    assert svc._is_windows() is True

    monkeypatch.setattr(sys, "platform", "linux")
    assert svc._is_darwin() is False
    assert svc._is_windows() is False


def test_resolve_hub_bin_returns_amplifier_browser_bridge_or_python_fallback() -> None:
    result = svc._resolve_hub_bin()
    assert svc.SERVICE_NAME in result or "python" in result


def test_hub_argv_tail_bakes_in_explicit_args_not_env_vars(tmp_path: Path) -> None:
    """The whole point of this module's design: host/port/token-file are explicit
    ARGUMENTS, never environment variables a service manager might not propagate."""
    token_file = tmp_path / "tokens.json"
    argv = svc._hub_argv_tail("100.1.2.3", 8900, token_file, None, None)
    assert argv[:5] == ["hub", "--host", "100.1.2.3", "--port", "8900"]
    assert "--token-file" in argv
    assert str(token_file) in argv


def test_hub_argv_tail_includes_optional_audit_log_and_command_timeout(tmp_path: Path) -> None:
    token_file = tmp_path / "tokens.json"
    audit_log = tmp_path / "audit.jsonl"
    argv = svc._hub_argv_tail("127.0.0.1", 8900, token_file, audit_log, 45.0)
    assert "--audit-log" in argv
    assert str(audit_log) in argv
    assert "--command-timeout" in argv
    assert "45.0" in argv


# ---------------------------------------------------------------------------
# systemd (Linux)
# ---------------------------------------------------------------------------


def test_systemd_install_writes_unit_with_explicit_args_and_enables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unit_dir = tmp_path / "systemd" / "user"
    unit_path = unit_dir / f"{svc.SERVICE_NAME}.service"
    monkeypatch.setattr(svc, "_SYSTEMD_UNIT_DIR", unit_dir)
    monkeypatch.setattr(svc, "_SYSTEMD_UNIT_PATH", unit_path)
    calls = _capturing_run(monkeypatch)
    token_file = tmp_path / "tokens.json"
    token_file.write_text("{}", encoding="utf-8")

    svc._systemd_install("100.124.126.19", 8900, token_file, tmp_path / "audit.jsonl", None)

    assert unit_path.exists()
    content = unit_path.read_text(encoding="utf-8")
    assert "hub" in content
    assert "--host 100.124.126.19" in content
    assert "--port 8900" in content
    assert str(token_file) in content
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", svc.SERVICE_NAME] in calls
    assert ["systemctl", "--user", "restart", svc.SERVICE_NAME] in calls


def test_systemd_uninstall_stops_disables_removes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit_path = unit_dir / f"{svc.SERVICE_NAME}.service"
    unit_path.write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(svc, "_SYSTEMD_UNIT_DIR", unit_dir)
    monkeypatch.setattr(svc, "_SYSTEMD_UNIT_PATH", unit_path)
    calls = _capturing_run(monkeypatch)

    svc._systemd_uninstall()

    assert ["systemctl", "--user", "stop", svc.SERVICE_NAME] in calls
    assert ["systemctl", "--user", "disable", svc.SERVICE_NAME] in calls
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert not unit_path.exists()


def test_systemd_start_stop_restart_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capturing_run(monkeypatch)
    svc._systemd_start()
    svc._systemd_stop()
    svc._systemd_restart()
    assert ["systemctl", "--user", "start", svc.SERVICE_NAME] in calls
    assert ["systemctl", "--user", "stop", svc.SERVICE_NAME] in calls
    assert ["systemctl", "--user", "restart", svc.SERVICE_NAME] in calls


def test_systemd_describe_not_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    unit_path = tmp_path / "does-not-exist.service"
    monkeypatch.setattr(svc, "_SYSTEMD_UNIT_PATH", unit_path)

    info = svc._systemd_describe()

    assert info.installed is False
    assert info.active is None
    assert info.supported is True
    assert info.platform == "linux"


def test_systemd_describe_installed_and_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    unit_path = tmp_path / f"{svc.SERVICE_NAME}.service"
    unit_path.write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(svc, "_SYSTEMD_UNIT_PATH", unit_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr=""),
    )

    info = svc._systemd_describe()

    assert info.installed is True
    assert info.active is True
    assert info.unit_path == unit_path


def test_systemd_describe_installed_but_inactive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    unit_path = tmp_path / f"{svc.SERVICE_NAME}.service"
    unit_path.write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(svc, "_SYSTEMD_UNIT_PATH", unit_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3, stdout="inactive\n", stderr=""),
    )

    info = svc._systemd_describe()

    assert info.installed is True
    assert info.active is False


# ---------------------------------------------------------------------------
# launchd (macOS) -- shape-only, mocked subprocess (this suite runs on Linux)
# ---------------------------------------------------------------------------


def test_launchd_install_writes_plist_with_separate_argv_strings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launchd bug this module exists to avoid repeating: each argv element must
    be its own <string>, never a single space-joined command."""
    import plistlib

    plist_dir = tmp_path / "LaunchAgents"
    plist_path = plist_dir / f"{svc._LAUNCHD_LABEL}.plist"
    monkeypatch.setattr(svc, "_LAUNCHD_PLIST_DIR", plist_dir)
    monkeypatch.setattr(svc, "_LAUNCHD_PLIST_PATH", plist_path)
    monkeypatch.setattr(svc, "_LAUNCHD_LOG_DIR", tmp_path / "Logs")
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    _capturing_run(monkeypatch)
    token_file = tmp_path / "tokens.json"
    token_file.write_text("{}", encoding="utf-8")

    svc._launchd_install("100.124.126.19", 8900, token_file, tmp_path / "audit.jsonl", None)

    assert plist_path.exists()
    data = plistlib.loads(plist_path.read_bytes())
    assert data["Label"] == svc._LAUNCHD_LABEL
    prog_args = data["ProgramArguments"]
    assert "hub" in prog_args
    assert "--host" in prog_args
    assert "100.124.126.19" in prog_args
    for arg in prog_args:
        assert " " not in arg, f"argv element must not contain spaces (embedded-arg trap): {arg!r}"


def test_launchd_install_boots_out_before_bootstrap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plist_dir = tmp_path / "LaunchAgents"
    plist_path = plist_dir / f"{svc._LAUNCHD_LABEL}.plist"
    monkeypatch.setattr(svc, "_LAUNCHD_PLIST_DIR", plist_dir)
    monkeypatch.setattr(svc, "_LAUNCHD_PLIST_PATH", plist_path)
    monkeypatch.setattr(svc, "_LAUNCHD_LOG_DIR", tmp_path / "Logs")
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    calls = _capturing_run(monkeypatch)
    token_file = tmp_path / "tokens.json"
    token_file.write_text("{}", encoding="utf-8")

    svc._launchd_install("127.0.0.1", 8900, token_file, None, None)

    bootout_index = next(i for i, c in enumerate(calls) if "bootout" in c)
    bootstrap_index = next(i for i, c in enumerate(calls) if "bootstrap" in c)
    assert bootout_index < bootstrap_index


def test_launchd_bootstrap_retries_through_the_teardown_race(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc.time, "sleep", lambda _s: None)
    state = {"n": 0}

    def fake_run(cmd, **kw):
        if cmd[1] == "bootstrap":
            state["n"] += 1
            rc = 5 if state["n"] < 3 else 0
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="Input/output error")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    svc._launchd_bootstrap(501)  # must not raise

    assert state["n"] == 3


def test_launchd_bootstrap_fails_loud_on_real_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="permission denied"),
    )

    with pytest.raises(RuntimeError, match="launchctl bootstrap failed"):
        svc._launchd_bootstrap(501)


def test_launchd_describe_not_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(svc, "_LAUNCHD_PLIST_PATH", tmp_path / "nope.plist")

    info = svc._launchd_describe()

    assert info.installed is False
    assert info.active is None


# ---------------------------------------------------------------------------
# describe_service() / public dispatch -- platform routing and Windows honesty
# ---------------------------------------------------------------------------


def test_describe_service_reports_windows_as_unsupported_with_named_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    info = describe_service()

    assert info.supported is False
    assert info.platform == "windows"
    assert "Windows" in info.detail
    assert "not implemented" in info.detail


def test_describe_service_reports_missing_systemctl_as_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(svc, "_have_systemctl", lambda: False)

    info = describe_service()

    assert info.supported is False
    assert "systemctl" in info.detail


@pytest.mark.parametrize(
    "op",
    [
        service_install,
        service_uninstall,
        service_start,
        service_stop,
        service_restart,
        service_status,
        service_logs,
    ],
)
def test_every_public_op_raises_service_unsupported_error_on_windows(
    monkeypatch: pytest.MonkeyPatch, op, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ServiceUnsupportedError, match="Windows"):
        if op is service_install:
            op("127.0.0.1", 8900, tmp_path / "tokens.json")
        else:
            op()


def test_service_install_dispatches_to_systemd_on_linux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(svc, "_have_systemctl", lambda: True)
    unit_dir = tmp_path / "systemd" / "user"
    monkeypatch.setattr(svc, "_SYSTEMD_UNIT_DIR", unit_dir)
    monkeypatch.setattr(svc, "_SYSTEMD_UNIT_PATH", unit_dir / f"{svc.SERVICE_NAME}.service")
    _capturing_run(monkeypatch)
    token_file = tmp_path / "tokens.json"
    token_file.write_text("{}", encoding="utf-8")

    info = service_install("127.0.0.1", 8900, token_file)

    assert info.platform == "linux"
    assert (unit_dir / f"{svc.SERVICE_NAME}.service").exists()


def test_service_install_uses_default_audit_log_when_not_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DEFAULT_SERVICE_AUDIT_LOG, never the hub CLI's own cwd-relative default --
    see module docstring for why a service can't rely on "current directory."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(svc, "_have_systemctl", lambda: True)
    unit_dir = tmp_path / "systemd" / "user"
    unit_path = unit_dir / f"{svc.SERVICE_NAME}.service"
    monkeypatch.setattr(svc, "_SYSTEMD_UNIT_DIR", unit_dir)
    monkeypatch.setattr(svc, "_SYSTEMD_UNIT_PATH", unit_path)
    fake_home = tmp_path / "home"
    monkeypatch.setattr(
        svc, "DEFAULT_SERVICE_AUDIT_LOG", fake_home / ".local/share/amplifier-browser-bridge/hub-audit.jsonl"
    )
    _capturing_run(monkeypatch)
    token_file = tmp_path / "tokens.json"
    token_file.write_text("{}", encoding="utf-8")

    service_install("127.0.0.1", 8900, token_file)

    content = unit_path.read_text(encoding="utf-8")
    assert "--audit-log" in content
    assert str(fake_home / ".local/share/amplifier-browser-bridge/hub-audit.jsonl") in content


def test_service_install_raises_service_unsupported_when_no_systemctl_and_not_darwin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(svc, "_have_systemctl", lambda: False)

    with pytest.raises(ServiceUnsupportedError, match="systemctl"):
        service_install("127.0.0.1", 8900, tmp_path / "tokens.json")
