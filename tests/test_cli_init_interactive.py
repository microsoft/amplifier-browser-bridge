"""Tests for `amplifier-browser-bridge init`'s guided, interactive flow -- the
one-command onboarding path (service install -> load extension -> pair ->
confirm) that replaces having to know and run `init`, `service install`,
`pair`, and `doctor` yourself, in order.

`click.testing.CliRunner` never gives the process a real tty, so every test
that wants to exercise the guided flow monkeypatches
`cli._stdin_is_interactive` directly -- that seam exists specifically so this
flow is reachable from a test at all (see its docstring). Tests that want the
*classic* print-only behavior rely on the CliRunner default (no monkeypatch,
no tty) exactly like the pre-existing `test_cli_init.py`/`test_cli_service.py`
suites already do -- proving that behavior is genuinely automatic, not just
documented.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from amplifier_browser_bridge import cli
from amplifier_browser_bridge.doctor import DoctorCheck
from amplifier_browser_bridge.service import ServiceInfo, ServiceUnsupportedError


def _fake_service_info(*, supported: bool = True, detail: str = "ok") -> ServiceInfo:
    return ServiceInfo(
        platform="linux" if supported else "windows",
        supported=supported,
        installed=supported,
        active=supported or None,
        unit_path=Path("/fake/unit") if supported else None,
        detail=detail,
    )


def _mock_hub_client(*, ticket_ok: bool = True, list_devices_raises: bool = False) -> MagicMock:
    """A stand-in for `cli.HubClient` used by both `_wait_for_hub_reachable`
    (`list_devices`) and `init`'s lazy pairing mint (`create_pairing`)."""
    client_cls = MagicMock()
    instance = client_cls.return_value
    if list_devices_raises:
        instance.list_devices = AsyncMock(side_effect=cli.HubError("hub unreachable"))
    else:
        instance.list_devices = AsyncMock(return_value=[])
    if ticket_ok:
        instance.create_pairing = AsyncMock(
            return_value={"ok": True, "ticket": "ABCDE-FGHIJ", "expires_in": 600, "persisted": True}
        )
    else:
        instance.create_pairing = AsyncMock(return_value={"ok": False, "error": "no token file"})
    return client_cls


# ---------------------------------------------------------------------------
# _resolve_interactivity -- pure unit tests, no CliRunner needed
# ---------------------------------------------------------------------------


def test_resolve_interactivity_non_interactive_flag_always_wins() -> None:
    assert cli._resolve_interactivity(yes=True, non_interactive=True) is False


def test_resolve_interactivity_yes_forces_guided_flow_without_a_tty() -> None:
    with patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=False):
        assert cli._resolve_interactivity(yes=True, non_interactive=False) is True


def test_resolve_interactivity_follows_tty_by_default() -> None:
    with patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=True):
        assert cli._resolve_interactivity(yes=False, non_interactive=False) is True
    with patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=False):
        assert cli._resolve_interactivity(yes=False, non_interactive=False) is False


def test_stdin_is_interactive_is_false_under_clirunner() -> None:
    """Sanity check for the test seam itself: CliRunner's captured streams are
    never real ttys, so the guided flow is unreachable without monkeypatching
    this -- exactly the property every test below relies on."""
    assert cli._stdin_is_interactive() is False


# ---------------------------------------------------------------------------
# --non-interactive forces the classic path even from a real terminal
# ---------------------------------------------------------------------------


def test_non_interactive_flag_forces_legacy_path_even_with_a_tty(tmp_path: Path) -> None:
    runner = CliRunner()
    with (
        patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=True),
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value=None),
        patch("amplifier_browser_bridge.cli.service_install") as mock_install,
    ):
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--non-interactive",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
        )
    assert result.exit_code == 0, result.output
    assert "Remaining steps (manual" in result.output
    mock_install.assert_not_called()


# ---------------------------------------------------------------------------
# Guided flow: declining the service offer
# ---------------------------------------------------------------------------


def test_guided_flow_declining_service_prints_full_manual_steps_and_installs_nothing(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    with (
        patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=True),
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch("amplifier_browser_bridge.cli.describe_service", return_value=_fake_service_info()),
        patch("amplifier_browser_bridge.cli.service_install") as mock_install,
    ):
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
            input="n\n",
        )
    assert result.exit_code == 0, result.output
    assert "Install and start it now?" in result.output
    mock_install.assert_not_called()
    # The decline path must leave the user with the EXACT foreground command,
    # not just a description of it -- same text the classic path prints.
    assert "amplifier-browser-bridge hub --host 100.1.2.3 --port 8900" in result.output
    assert "Remaining steps (manual" in result.output


# ---------------------------------------------------------------------------
# Guided flow: platform has no service support at all (Windows)
# ---------------------------------------------------------------------------


def test_guided_flow_skips_the_prompt_entirely_when_service_is_unsupported(tmp_path: Path) -> None:
    runner = CliRunner()
    windows_detail = (
        "service management is not implemented for Windows in this release -- there is no "
        "systemd/launchd equivalent this module drives there yet."
    )
    with (
        patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=True),
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch(
            "amplifier_browser_bridge.cli.describe_service",
            return_value=_fake_service_info(supported=False, detail=windows_detail),
        ),
        patch("amplifier_browser_bridge.cli.service_install") as mock_install,
    ):
        # No `input=` at all -- if this tried to prompt, CliRunner would raise
        # rather than hang, which is exactly the regression this test guards.
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
    assert "Install and start it now?" not in result.output
    assert windows_detail in result.output
    mock_install.assert_not_called()
    assert "Remaining steps (manual" in result.output


# ---------------------------------------------------------------------------
# Guided flow: --yes automates the service but never blocks on the browser step
# ---------------------------------------------------------------------------


def test_yes_flag_installs_service_then_prints_manual_pair_step_without_minting(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    fake_info = _fake_service_info()
    mock_client_cls = _mock_hub_client()
    with (
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info) as mock_install,
        patch("amplifier_browser_bridge.cli.HubClient", mock_client_cls),
    ):
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--yes",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
        )
    assert result.exit_code == 0, result.output
    mock_install.assert_called_once()
    assert "Installed and started" in result.output
    assert "Confirmed: hub reachable" in result.output
    assert "Load the extension" in result.output
    assert "amplifier-browser-bridge pair" in result.output
    assert "amplifier-browser-bridge doctor" in result.output
    # --yes never blocks waiting on the browser step, so no ticket is minted.
    mock_client_cls.return_value.create_pairing.assert_not_called()


# ---------------------------------------------------------------------------
# Guided flow: full happy path, including the lazy-mint TTL fix
# ---------------------------------------------------------------------------


def test_guided_flow_full_happy_path_mints_ticket_lazily_and_confirms_via_doctor(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    fake_info = _fake_service_info()
    mock_client_cls = _mock_hub_client()
    all_ok_checks = [DoctorCheck("hub_reachable", "ok", "hub reachable")]
    with (
        patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=True),
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.HubClient", mock_client_cls),
        patch("amplifier_browser_bridge.cli.run_doctor", new=AsyncMock(return_value=all_ok_checks)),
    ):
        # Three confirms, in order: install the service? / extension loaded? /
        # entered the code and paired? -- empty lines accept each default (Y).
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
            input="\n\n\n",
        )
    assert result.exit_code == 0, result.output
    assert "Installed and started" in result.output
    assert "Confirmed: hub reachable" in result.output
    assert "Loaded, and its Settings page is open?" in result.output
    # The minted code, its TTL, and the re-mint command all shown together.
    assert "ABCDE-FGHIJ@100.1.2.3:8900" in result.output
    assert "valid 600s" in result.output
    assert "amplifier-browser-bridge pair" in result.output
    assert "Entered the code and clicked Pair?" in result.output
    assert "[ok]" in result.output
    assert "All checks passed" in result.output
    mock_client_cls.return_value.create_pairing.assert_awaited_once()


def test_guided_flow_final_doctor_failure_exits_nonzero(tmp_path: Path) -> None:
    """A [FAIL] on the final auto-run doctor (e.g. pairing genuinely wasn't
    finished) must be reported the same way the standalone `doctor` command
    reports it -- non-zero exit, not swallowed into a cheerful success."""
    runner = CliRunner()
    fake_info = _fake_service_info()
    mock_client_cls = _mock_hub_client()
    failing_checks = [
        DoctorCheck("hub_reachable", "ok", "hub reachable"),
        DoctorCheck("device_connected", "fail", "no browser device has ever connected to this hub"),
    ]
    with (
        patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=True),
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.HubClient", mock_client_cls),
        patch("amplifier_browser_bridge.cli.run_doctor", new=AsyncMock(return_value=failing_checks)),
    ):
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
            input="\n\n\n",
        )
    assert result.exit_code != 0
    assert "[FAIL]" in result.output
    assert "one or more checks failed" in result.output


def test_guided_flow_declining_after_loading_never_mints_a_ticket(tmp_path: Path) -> None:
    """Proves the TTL fix structurally: if the user isn't ready yet, no ticket
    is minted at all -- there is nothing to expire."""
    runner = CliRunner()
    fake_info = _fake_service_info()
    mock_client_cls = _mock_hub_client()
    with (
        patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=True),
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.HubClient", mock_client_cls),
    ):
        # install? yes (default) / loaded? no
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
            input="\nn\n",
        )
    assert result.exit_code == 0, result.output
    assert "No problem -- whenever you're ready" in result.output
    assert "amplifier-browser-bridge pair" in result.output
    mock_client_cls.return_value.create_pairing.assert_not_called()


def test_guided_flow_declining_after_getting_code_skips_doctor(tmp_path: Path) -> None:
    runner = CliRunner()
    fake_info = _fake_service_info()
    mock_client_cls = _mock_hub_client()
    with (
        patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=True),
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.HubClient", mock_client_cls),
        patch("amplifier_browser_bridge.cli.run_doctor", new=AsyncMock()) as mock_doctor,
    ):
        # install? yes / loaded? yes (mints ticket) / entered+paired? no
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
            input="\n\nn\n",
        )
    assert result.exit_code == 0, result.output
    assert "Check any time with:" in result.output
    assert "amplifier-browser-bridge doctor" in result.output
    mock_client_cls.return_value.create_pairing.assert_awaited_once()
    mock_doctor.assert_not_called()


# ---------------------------------------------------------------------------
# Partial failure: service installs but never becomes reachable
# ---------------------------------------------------------------------------


def test_yes_flag_reports_partial_failure_when_hub_never_becomes_reachable(tmp_path: Path) -> None:
    runner = CliRunner()
    fake_info = _fake_service_info()
    mock_client_cls = _mock_hub_client(list_devices_raises=True)
    with (
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.HubClient", mock_client_cls),
        patch("amplifier_browser_bridge.cli._SERVICE_READY_TIMEOUT_S", 0.05),
        patch("amplifier_browser_bridge.cli._SERVICE_READY_POLL_S", 0.01),
    ):
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--yes",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
        )
    assert result.exit_code != 0
    assert "never became reachable" in result.output
    assert "still valid" in result.output
    assert "service status" in result.output
    assert "service logs" in result.output
    assert "amplifier-browser-bridge pair" in result.output


def test_yes_flag_reports_failure_when_service_install_itself_raises(tmp_path: Path) -> None:
    runner = CliRunner()
    fake_info = _fake_service_info()
    with (
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info),
        patch(
            "amplifier_browser_bridge.cli.service_install",
            side_effect=ServiceUnsupportedError("systemctl not found"),
        ),
    ):
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--yes",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
        )
    assert result.exit_code != 0
    assert "could not install the hub service" in result.output
    assert "unaffected -- run the hub directly instead" in result.output
