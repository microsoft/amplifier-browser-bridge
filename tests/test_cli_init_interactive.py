"""Tests for `amplifier-browser-bridge init`'s guided, interactive flow -- the
one-command onboarding path (service install -> add browser via a code-carrying
link -> auto-detect the connection -> confirm) that replaces having to know and
run `init`, `service install`, `pair`, and `doctor` yourself, in order.

Onboarding-v2 (real-run bug report + two independent council reviews, 2026-08):
the flow used to ask TWO separate `[Y/n]` prompts after the service-install
decision -- "Loaded, and its Settings page is open?" and "Entered the code and
clicked Pair?" -- and only minted the pairing code lazily, after the first of
those. Both are gone: the code is minted right after the hub is confirmed
reachable (so the ONE link handed to the user already carries it), and the flow
now WATCHES `list_devices()` for the browser to actually connect, continuing on
its own the moment it does. Tests below cover both the auto-advance path (a live
device appears within the watch window) and the honest timeout-fallback path (it
doesn't, so the flow degrades to one manual confirm rather than hanging forever).

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

from contextlib import ExitStack
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


_LIVE_DEVICE = {"device_id": "dev-1", "label": "edge-macos", "platform": "Darwin", "tier": "live"}


def _mock_hub_client(
    *, ticket_ok: bool = True, list_devices_raises: bool = False, devices: list[dict] | None = None
) -> MagicMock:
    """A stand-in for `cli.HubClient` used by `_wait_for_hub_reachable`,
    `_watch_for_device_connection` (both call `list_devices`), and `init`'s
    pairing mint (`create_pairing`).

    `devices` is returned verbatim from every `list_devices()` call -- pass
    `[_LIVE_DEVICE]` to make the watch loop auto-advance immediately, or leave
    it as `[]` (the default) to exercise the timeout-fallback path.
    """
    client_cls = MagicMock()
    instance = client_cls.return_value
    if list_devices_raises:
        instance.list_devices = AsyncMock(side_effect=cli.HubError("hub unreachable"))
    else:
        instance.list_devices = AsyncMock(return_value=devices if devices is not None else [])
    if ticket_ok:
        instance.create_pairing = AsyncMock(
            return_value={"ok": True, "ticket": "ABCDE-FGHIJ", "expires_in": 600, "persisted": True}
        )
    else:
        instance.create_pairing = AsyncMock(return_value={"ok": False, "error": "no token file"})
    return client_cls


def _patch_watch_fast(**overrides):
    """Patch the device-watch loop's timing constants down to test-speed values.
    Individual tests override `timeout`/`poll`/`heartbeat` via kwargs when they
    need to force the timeout-fallback branch instead of the instant-match one.
    """
    values = {
        "amplifier_browser_bridge.cli._DEVICE_WATCH_TIMEOUT_S": overrides.get("timeout", 5.0),
        "amplifier_browser_bridge.cli._DEVICE_WATCH_POLL_S": overrides.get("poll", 0.01),
        "amplifier_browser_bridge.cli._DEVICE_WATCH_HEARTBEAT_S": overrides.get("heartbeat", 100.0),
    }
    return [patch(target, value) for target, value in values.items()]


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
# Guided flow: --yes automates the service but never blocks on anything past it
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
# Guided flow: full happy path -- mints eagerly, leads with the link, then
# AUTO-ADVANCES the moment the hub observes a live device (no manual confirm).
# ---------------------------------------------------------------------------


def test_guided_flow_full_happy_path_auto_advances_via_device_watch_and_confirms_via_doctor(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    fake_info = _fake_service_info()
    mock_client_cls = _mock_hub_client(devices=[_LIVE_DEVICE])
    all_ok_checks = [DoctorCheck("hub_reachable", "ok", "hub reachable")]
    patches = [
        patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=True),
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.HubClient", mock_client_cls),
        patch("amplifier_browser_bridge.cli.run_doctor", new=AsyncMock(return_value=all_ok_checks)),
        *_patch_watch_fast(),
    ]
    with _apply(patches):
        # Exactly ONE confirm now: the service-install offer. Empty line accepts
        # the default (Y). Nothing else should prompt -- a live device is
        # observed on the very first poll.
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
            input="\n",
        )
    assert result.exit_code == 0, result.output
    assert "Installed and started" in result.output
    assert "Confirmed: hub reachable" in result.output
    # The pairing link leads -- carries the code AND an expiry in the fragment.
    assert "#pair=ABCDE-FGHIJ@100.1.2.3:8900&exp=" in result.output
    assert "Waiting for the browser to connect" in result.output
    assert "Connected: device dev-1 (edge-macos, Darwin) -- continuing automatically." in result.output
    # The two old prompts are GONE.
    assert "Loaded, and its Settings page is open?" not in result.output
    assert "Entered the code and clicked Pair?" not in result.output
    assert "[ok]" in result.output
    assert "All checks passed" in result.output
    mock_client_cls.return_value.create_pairing.assert_awaited_once()


def test_guided_flow_final_doctor_failure_exits_nonzero(tmp_path: Path) -> None:
    """A [FAIL] on the final auto-run doctor (e.g. pairing genuinely wasn't
    finished) must be reported the same way the standalone `doctor` command
    reports it -- non-zero exit, not swallowed into a cheerful success."""
    runner = CliRunner()
    fake_info = _fake_service_info()
    mock_client_cls = _mock_hub_client(devices=[_LIVE_DEVICE])
    failing_checks = [
        DoctorCheck("hub_reachable", "ok", "hub reachable"),
        DoctorCheck("device_connected", "fail", "no browser device has ever connected to this hub"),
    ]
    patches = [
        patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=True),
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.HubClient", mock_client_cls),
        patch("amplifier_browser_bridge.cli.run_doctor", new=AsyncMock(return_value=failing_checks)),
        *_patch_watch_fast(),
    ]
    with _apply(patches):
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
            input="\n",
        )
    assert result.exit_code != 0
    assert "[FAIL]" in result.output
    assert "one or more checks failed" in result.output


# ---------------------------------------------------------------------------
# Guided flow: no device connects within the watch window -> honest timeout,
# then ONE manual fallback confirmation (never a silent hang, never a silent
# jump-cut past the user).
# ---------------------------------------------------------------------------


def test_guided_flow_watch_timeout_falls_back_to_one_manual_confirm_and_then_runs_doctor(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    fake_info = _fake_service_info()
    mock_client_cls = _mock_hub_client(devices=[])  # never live -- forces the timeout branch
    all_ok_checks = [DoctorCheck("hub_reachable", "ok", "hub reachable")]
    patches = [
        patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=True),
        patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3"),
        patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info),
        patch("amplifier_browser_bridge.cli.HubClient", mock_client_cls),
        patch("amplifier_browser_bridge.cli.run_doctor", new=AsyncMock(return_value=all_ok_checks)),
        *_patch_watch_fast(timeout=0.03, poll=0.01),
    ]
    with _apply(patches):
        # install? yes (default) / fallback "still there?" -> yes (default)
        result = runner.invoke(
            cli.main,
            [
                "init",
                "--dest",
                str(tmp_path / "extension"),
                "--token-file",
                str(tmp_path / "tokens.json"),
            ],
            input="\n\n",
        )
    assert result.exit_code == 0, result.output
    assert "Waiting for the browser to connect" in result.output
    assert "Connected: device" not in result.output  # no device ever showed up live
    assert "Still there? Finished loading the extension and pairing it?" in result.output
    assert "All checks passed" in result.output


def test_guided_flow_declining_the_timeout_fallback_confirm_skips_doctor(tmp_path: Path) -> None:
    runner = CliRunner()
    fake_info = _fake_service_info()
    mock_client_cls = _mock_hub_client(devices=[])
    with ExitStack() as stack:
        stack.enter_context(patch("amplifier_browser_bridge.cli._stdin_is_interactive", return_value=True))
        stack.enter_context(
            patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.1.2.3")
        )
        stack.enter_context(patch("amplifier_browser_bridge.cli.describe_service", return_value=fake_info))
        stack.enter_context(patch("amplifier_browser_bridge.cli.service_install", return_value=fake_info))
        stack.enter_context(patch("amplifier_browser_bridge.cli.HubClient", mock_client_cls))
        mock_doctor = stack.enter_context(patch("amplifier_browser_bridge.cli.run_doctor", new=AsyncMock()))
        for p in _patch_watch_fast(timeout=0.03, poll=0.01):
            stack.enter_context(p)

        # install? yes / fallback "still there?" -> no
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
    assert "No problem -- check any time with:" in result.output
    assert "amplifier-browser-bridge doctor" in result.output
    mock_doctor.assert_not_called()

    # The onboarding-local audit log recorded the abandonment -- see
    # `_onboarding_audit_log`'s module-level design-decision comment in cli.py.
    # token_file itself is tokens.json; the audit log sits beside it.
    onboarding_log = (tmp_path / "tokens.json").parent / "onboarding-audit.jsonl"
    assert onboarding_log.exists()
    contents = onboarding_log.read_text(encoding="utf-8")
    assert "onboarding_watch_started" in contents
    assert "onboarding_watch_timeout" in contents
    assert "onboarding_manual_fallback_declined" in contents


def _apply(patches: list) -> ExitStack:
    """Tiny helper: enter/exit a LIST of already-constructed `patch(...)` context
    managers together, since `unittest.mock.patch` objects aren't directly
    unpackable into a single `with (...)` tuple the way inline `patch(...)`
    calls are elsewhere in this file."""
    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


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
