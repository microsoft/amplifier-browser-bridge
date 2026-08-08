"""Regression test for a real bug found while implementing A1: `init`'s
click decorators (`--dest`/--token-file/--force/--hub-host/--hub-port) were
misattached to `_warn_divergent_token_siblings` (an internal helper, never
meant to be a command) after the abb -> amplifier-browser-bridge rename, leaving `init`
itself with NO decorator at all -- `amplifier-browser-bridge init` was not a registered CLI
command. `python -c "from amplifier_browser_bridge import cli; cli.main.commands.keys()"`
listed `-warn-divergent-token-siblings` (with click's dash-mangled name) instead
of `init`.

Also covers the A1/A4 fixes' CLI-level wiring: `hub --host` default, and the
`kill-switch` command group existing at all.
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from amplifier_browser_bridge import cli


def test_init_is_a_registered_command() -> None:
    """The actual regression: before the fix, `init` was absent from
    `cli.main.commands` entirely (decorators were misattached to
    `_warn_divergent_token_siblings`, a private helper)."""
    assert "init" in cli.main.commands
    assert "_warn_divergent_token_siblings" not in cli.main.commands
    assert "-warn-divergent-token-siblings" not in cli.main.commands


def test_init_runs_end_to_end_and_prints_remaining_steps(tmp_path) -> None:
    runner = CliRunner()
    dest = tmp_path / "extension"
    token_file = tmp_path / "tokens.json"

    with patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value=None):
        result = runner.invoke(
            cli.main,
            ["init", "--dest", str(dest), "--token-file", str(token_file), "--hub-port", "8901"],
        )

    assert result.exit_code == 0, result.output
    assert "Generated new hub token" in result.output
    assert "Remaining steps" in result.output
    assert token_file.is_file()


def test_init_falls_back_to_loopback_and_warns_when_tailscale_undetected(tmp_path) -> None:
    runner = CliRunner()
    with patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value=None):
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
    assert "127.0.0.1" in result.output
    assert "NOT reachable from another device" in result.output


def test_init_auto_detects_tailscale_ip_when_available(tmp_path) -> None:
    runner = CliRunner()
    with patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.124.126.19"):
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
    assert "100.124.126.19" in result.output
    assert "auto-detected" in result.output


def test_init_warns_loudly_when_explicit_hub_host_is_a_wildcard(tmp_path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "init",
            "--dest",
            str(tmp_path / "extension"),
            "--token-file",
            str(tmp_path / "tokens.json"),
            "--hub-host",
            "0.0.0.0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert "EVERY network interface" in result.output


def test_hub_command_defaults_to_loopback_only() -> None:
    """A1 fix: --host must no longer default to 0.0.0.0."""
    param = next(p for p in cli.hub.params if p.name == "host")
    assert param.default == "127.0.0.1"


def test_kill_switch_command_group_is_registered() -> None:
    """A4 fix: the kill switch must be reachable from the CLI."""
    assert "kill-switch" in cli.main.commands
    assert {"engage", "disengage", "status"} <= set(cli.kill_switch.commands.keys())
