"""Tests for cli.py's `service` command group -- install/uninstall/start/stop/
restart/status/logs, plus the refactored `_resolve_hub_host` shared with `init`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from amplifier_browser_bridge import cli
from amplifier_browser_bridge.service import ServiceInfo, ServiceUnsupportedError


def test_service_group_and_subcommands_are_registered() -> None:
    assert "service" in cli.main.commands
    subs = set(cli.service_group.commands.keys())
    assert {"install", "uninstall", "start", "stop", "restart", "status", "logs"} <= subs


def test_resolve_hub_host_explicit_wins() -> None:
    host, note = cli._resolve_hub_host("100.9.9.9")
    assert host == "100.9.9.9"
    assert note is None


def test_resolve_hub_host_auto_detects_tailscale() -> None:
    with patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"):
        host, note = cli._resolve_hub_host(None)
    assert host == "100.1.2.3"
    assert note is not None
    assert "auto-detected" in note


def test_resolve_hub_host_falls_back_to_loopback() -> None:
    with patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value=None):
        host, note = cli._resolve_hub_host(None)
    assert host == "127.0.0.1"
    assert note is not None
    assert "NOT reachable from another device" in note


def test_service_install_cmd_generates_token_if_missing_and_calls_service_install(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    token_file = tmp_path / "tokens.json"
    fake_info = ServiceInfo(
        platform="linux",
        supported=True,
        installed=True,
        active=True,
        unit_path=tmp_path / "unit",
        detail="ok",
    )

    with (
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info) as mock_install,
    ):
        result = runner.invoke(
            cli.main,
            ["service", "install", "--token-file", str(token_file), "--port", "8900"],
        )

    assert result.exit_code == 0, result.output
    assert token_file.is_file(), "service install must ensure a token exists, same as init"
    assert "Generated new hub token" in result.output
    assert "Installed and started" in result.output
    mock_install.assert_called_once()
    args, _kwargs = mock_install.call_args
    assert args[0] == "100.1.2.3"
    assert args[1] == 8900
    assert Path(args[2]) == token_file.resolve()


def test_service_install_cmd_persists_the_resolved_hub_location(tmp_path: Path) -> None:
    """`service install` shares `_resolve_hub_host` with `init` and must
    persist the decision too -- e.g. a user who skipped `init`'s guided flow
    and ran `service install` directly still gets a working default for
    other commands (a bare `devices`, the MCP server, the tool module)."""
    from amplifier_browser_bridge.hub_location import read_hub_location

    runner = CliRunner()
    token_file = tmp_path / "tokens.json"
    location_file = tmp_path / "hub_location.json"
    fake_info = ServiceInfo(
        platform="linux",
        supported=True,
        installed=True,
        active=True,
        unit_path=tmp_path / "unit",
        detail="ok",
    )

    with (
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.5.5.5"),
        patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info),
        patch("amplifier_browser_bridge.hub_location.resolve_hub_location_file", return_value=location_file),
    ):
        result = runner.invoke(
            cli.main,
            ["service", "install", "--token-file", str(token_file), "--port", "8901"],
        )

    assert result.exit_code == 0, result.output
    location = read_hub_location(location_file)
    assert location is not None
    assert location.host == "100.5.5.5"
    assert location.port == 8901


def test_service_install_cmd_reuses_existing_token_file(tmp_path: Path) -> None:
    runner = CliRunner()
    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"default": "existing-token", "devices": {}}), encoding="utf-8")
    fake_info = ServiceInfo(
        platform="linux",
        supported=True,
        installed=True,
        active=True,
        unit_path=tmp_path / "unit",
        detail="ok",
    )

    with (
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value=None),
        patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info),
    ):
        result = runner.invoke(cli.main, ["service", "install", "--token-file", str(token_file)])

    assert result.exit_code == 0, result.output
    assert "Generated new hub token" not in result.output
    data = json.loads(token_file.read_text(encoding="utf-8"))
    assert data["default"] == "existing-token"  # untouched


def test_service_install_cmd_warns_on_wildcard_host(tmp_path: Path) -> None:
    runner = CliRunner()
    fake_info = ServiceInfo(
        platform="linux",
        supported=True,
        installed=True,
        active=True,
        unit_path=tmp_path / "unit",
        detail="ok",
    )
    with patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info):
        result = runner.invoke(
            cli.main,
            [
                "service",
                "install",
                "--host",
                "0.0.0.0",
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
        )
    assert result.exit_code == 0, result.output
    # wildcard_bind_warning is printed to stderr in CliRunner's mixed stream by default
    assert "EVERY network interface" in result.output


def test_service_install_cmd_translates_unsupported_error(tmp_path: Path) -> None:
    runner = CliRunner()
    with (
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch(
            "amplifier_browser_bridge.cli.service_install",
            side_effect=ServiceUnsupportedError("Windows is not implemented"),
        ),
    ):
        result = runner.invoke(
            cli.main,
            ["service", "install", "--token-file", str(tmp_path / "tokens.json")],
        )
    assert result.exit_code != 0
    assert "Windows is not implemented" in result.output


@pytest.mark.parametrize(
    ("subcommand", "patched_name", "expected_word"),
    [
        (["service", "uninstall"], "service_uninstall", "Removed"),
        (["service", "start"], "service_start", "Started"),
        (["service", "stop"], "service_stop", "Stopped"),
        (["service", "restart"], "service_restart", "Restarted"),
    ],
)
def test_service_lifecycle_subcommands_dispatch_and_report(
    subcommand: list[str], patched_name: str, expected_word: str
) -> None:
    runner = CliRunner()
    with patch(f"amplifier_browser_bridge.cli.{patched_name}") as mock_fn:
        result = runner.invoke(cli.main, subcommand)
    assert result.exit_code == 0, result.output
    mock_fn.assert_called_once()
    assert expected_word in result.output


@pytest.mark.parametrize(
    ("subcommand", "patched_name"),
    [
        (["service", "uninstall"], "service_uninstall"),
        (["service", "start"], "service_start"),
        (["service", "stop"], "service_stop"),
        (["service", "restart"], "service_restart"),
        (["service", "logs"], "service_logs"),
    ],
)
def test_service_lifecycle_subcommands_translate_unsupported_error(
    subcommand: list[str], patched_name: str
) -> None:
    runner = CliRunner()
    with patch(
        f"amplifier_browser_bridge.cli.{patched_name}",
        side_effect=ServiceUnsupportedError("not supported here"),
    ):
        result = runner.invoke(cli.main, subcommand)
    assert result.exit_code != 0
    assert "not supported here" in result.output


def test_service_status_cmd_prints_structured_info_and_raw_status(tmp_path: Path) -> None:
    runner = CliRunner()
    fake_info = ServiceInfo(
        platform="linux",
        supported=True,
        installed=True,
        active=True,
        unit_path=tmp_path / "unit",
        detail="installed and active",
    )
    with (
        patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.service_status") as mock_status,
    ):
        result = runner.invoke(cli.main, ["service", "status"])
    assert result.exit_code == 0, result.output
    assert "installed: True" in result.output
    assert "active: True" in result.output
    mock_status.assert_called_once()


def test_service_status_cmd_skips_raw_status_when_not_installed() -> None:
    runner = CliRunner()
    fake_info = ServiceInfo(
        platform="linux", supported=True, installed=False, active=None, unit_path=None, detail="not installed"
    )
    with (
        patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.service_status") as mock_status,
    ):
        result = runner.invoke(cli.main, ["service", "status"])
    assert result.exit_code == 0, result.output
    mock_status.assert_not_called()


def test_init_recommends_service_install_as_the_primary_step(tmp_path: Path) -> None:
    """init's printed step 1 must recommend `service install`, not a bare foreground
    `hub` invocation, as THE recommendation -- the foreground command survives too,
    but as the secondary "run it directly instead" option."""
    runner = CliRunner()
    with patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"):
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
        )
    assert result.exit_code == 0, result.output
    assert "amplifier-browser-bridge service install --host 100.1.2.3 --port 8900" in result.output
    assert "survives logout and reboot" in result.output
    # the foreground fallback must still be documented
    assert "amplifier-browser-bridge hub --host 100.1.2.3 --port 8900" in result.output
    assert "run it directly in this terminal instead" in result.output
