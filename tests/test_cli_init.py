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

import re
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


def _extract_hosts(output: str) -> tuple[str, str]:
    """Pulls the host `init` printed in step 1's `hub --host` invocation and the
    host it printed in step 4's `doctor --hub-url` invocation, so a test can
    assert they agree without depending on exact surrounding wording."""
    hub_match = re.search(r"amplifier-browser-bridge hub --host (\S+) --port", output)
    doctor_match = re.search(r"doctor --hub-url ws://([^:/]+):", output)
    assert hub_match, f"could not find step 1's `hub --host` line in output:\n{output}"
    assert doctor_match, f"could not find step 4's `doctor --hub-url` line in output:\n{output}"
    return hub_match.group(1), doctor_match.group(1)


def test_init_doctor_url_agrees_with_the_hub_host_it_just_printed_explicit_host(tmp_path) -> None:
    """Regression test: step 4's `doctor --hub-url` used to hardcode ws://127.0.0.1,
    independent of whatever host step 1 told the user to bind the hub to -- so
    following the printed steps verbatim always produced `[FAIL] hub_reachable`
    whenever step 1 bound anything other than loopback (e.g. a real Tailscale IP).
    """
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
            "100.124.126.19",
            "--hub-port",
            "8900",
        ],
    )

    assert result.exit_code == 0, result.output
    hub_host, doctor_host = _extract_hosts(result.output)
    assert hub_host == "100.124.126.19"
    assert doctor_host == hub_host, (
        f"step 1 told the user to bind the hub to {hub_host!r} but step 4's doctor "
        f"check points at {doctor_host!r} -- following the steps verbatim would fail"
    )


def test_init_doctor_url_agrees_with_auto_detected_tailscale_host(tmp_path) -> None:
    """Same regression, via the auto-detect path (no --hub-host given) -- this is
    exactly the scenario from the bug report: `tailscale ip -4` resolves a real
    tailnet IP for step 1, and step 4 must point at that same IP, not loopback."""
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
                "--hub-port",
                "8900",
            ],
        )

    assert result.exit_code == 0, result.output
    hub_host, doctor_host = _extract_hosts(result.output)
    assert hub_host == "100.124.126.19"
    assert doctor_host == hub_host


def test_init_doctor_url_agrees_with_loopback_fallback(tmp_path) -> None:
    """Same regression, via the loopback-fallback path (Tailscale undetected) --
    both steps must agree on 127.0.0.1 here, same as any other host."""
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
    hub_host, doctor_host = _extract_hosts(result.output)
    assert hub_host == "127.0.0.1"
    assert doctor_host == hub_host


def test_hub_command_defaults_to_loopback_only() -> None:
    """A1 fix: --host must no longer default to 0.0.0.0."""
    param = next(p for p in cli.hub.params if p.name == "host")
    assert param.default == "127.0.0.1"


def test_kill_switch_command_group_is_registered() -> None:
    """A4 fix: the kill switch must be reachable from the CLI."""
    assert "kill-switch" in cli.main.commands
    assert {"engage", "disengage", "status"} <= set(cli.kill_switch.commands.keys())


def test_init_persists_the_resolved_hub_location(tmp_path) -> None:
    """The fix: `init` resolving a host must PERSIST it (host, port) so a
    later, unrelated command (a bare `devices`, the MCP server, the tool
    module) can read it back instead of falling through to a hardcoded
    loopback default -- the exact bug reported (`init` printed a working
    `devices` command that then crashed with ConnectionRefusedError against
    127.0.0.1, because nothing recorded where `init` had actually put the
    hub)."""
    from amplifier_browser_bridge.hub_location import read_hub_location

    runner = CliRunner()
    location_file = tmp_path / "hub_location.json"
    with (
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.124.126.19"),
        patch("amplifier_browser_bridge.hub_location.resolve_hub_location_file", return_value=location_file),
    ):
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
                "--hub-port",
                "8900",
            ],
        )

    assert result.exit_code == 0, result.output
    location = read_hub_location(location_file)
    assert location is not None
    assert location.host == "100.124.126.19"
    assert location.port == 8900
