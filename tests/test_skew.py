"""skew.py -- bidirectional command-set skew detection (Tier 0 handshake).

Covers: pre-handshake (device never reported a command set at all) flagged
as a distinct, definitively-stale state; bidirectional skew naming the
correct side (extension-behind vs hub-behind); and the dispatch-time
fast-fail helper's deliberate no-op for the unknown case (see skew.py's
module docstring for why -- blocking `reload` for a pre-Tier-0 device would
break the very mechanism the update story depends on).
"""

from __future__ import annotations

from amplifier_browser_bridge.skew import capability_error, compute_skew, describe_skew

_HUB_COMMANDS = frozenset({"snapshot", "click", "type", "reload", "tabs"})


def test_pre_handshake_device_is_flagged_stale_not_unknown() -> None:
    """A device that never sent `commands` at all -- every extension shipped
    before this feature -- is `known=False`: a distinct, definitively-stale
    state, never conflated with "in sync" or a spurious per-command diff."""
    report = compute_skew(None, _HUB_COMMANDS)
    assert report.known is False
    assert report.device_behind == frozenset()
    assert report.hub_behind == frozenset()
    assert report.in_sync is False


def test_pre_handshake_description_says_stale_and_names_the_fix() -> None:
    report = compute_skew(None, _HUB_COMMANDS)
    message = describe_skew(report)
    assert message is not None
    assert "never reported a command set" in message
    assert "stale" in message
    assert "browser_update_extension" in message


def test_in_sync_device_reports_no_skew() -> None:
    report = compute_skew(frozenset(_HUB_COMMANDS), _HUB_COMMANDS)
    assert report.known is True
    assert report.in_sync is True
    assert describe_skew(report) is None


def test_extension_behind_hub_names_the_extension_as_the_problem() -> None:
    """The common case: a device is missing a command the hub knows about --
    the extension needs updating."""
    device_commands = frozenset({"snapshot", "click", "tabs"})  # missing "type", "reload"
    report = compute_skew(device_commands, _HUB_COMMANDS)
    assert report.known is True
    assert report.device_behind == frozenset({"type", "reload"})
    assert report.hub_behind == frozenset()
    assert report.in_sync is False

    message = describe_skew(report)
    assert message is not None
    assert "EXTENSION is behind" in message
    assert "HUB is behind" not in message
    assert "type" in message and "reload" in message


def test_hub_behind_extension_names_the_hub_as_the_problem() -> None:
    """The less-common case: the device reports a command the hub's own
    vocabulary doesn't recognize -- the HUB needs updating/restarting, not
    the extension."""
    device_commands = frozenset({"snapshot", "click", "type", "reload", "tabs", "a_future_command"})
    report = compute_skew(device_commands, _HUB_COMMANDS)
    assert report.known is True
    assert report.device_behind == frozenset()
    assert report.hub_behind == frozenset({"a_future_command"})
    assert report.in_sync is False

    message = describe_skew(report)
    assert message is not None
    assert "HUB is behind" in message
    assert "EXTENSION is behind" not in message
    assert "a_future_command" in message


def test_bidirectional_skew_names_both_sides_at_once() -> None:
    device_commands = frozenset(
        {"snapshot", "click", "a_future_command"}
    )  # missing type/reload/tabs, extra one
    report = compute_skew(device_commands, _HUB_COMMANDS)
    assert report.device_behind == frozenset({"type", "reload", "tabs"})
    assert report.hub_behind == frozenset({"a_future_command"})

    message = describe_skew(report)
    assert message is not None
    assert "EXTENSION is behind" in message
    assert "HUB is behind" in message


def test_to_summary_is_json_friendly_and_sorted() -> None:
    device_commands = frozenset({"snapshot"})
    report = compute_skew(device_commands, _HUB_COMMANDS)
    summary = report.to_summary()
    assert summary["known"] is True
    assert summary["in_sync"] is False
    assert summary["device_behind"] == sorted(_HUB_COMMANDS - device_commands)
    assert summary["hub_behind"] == []
    assert isinstance(summary["summary"], str)


# ---------------------------------------------------------------------------
# capability_error -- the dispatch-time fast-fail
# ---------------------------------------------------------------------------


def test_capability_error_fast_fails_a_known_missing_command() -> None:
    """The core Tier 0 fast-fail: a device that POSITIVELY reported a command
    set not including `command` gets refused before dispatch, naming the
    extension as behind -- never background.js's own generic fallback."""
    device_commands = frozenset({"snapshot", "click"})
    error = capability_error("reload", device_commands)
    assert error is not None
    assert error["ok"] is False
    assert error["reason_code"] == "device_command_unsupported"
    assert "reload" in error["error"]
    assert "EXTENSION is behind" in error["error"]


def test_capability_error_allows_a_known_supported_command() -> None:
    device_commands = frozenset({"snapshot", "click", "reload"})
    assert capability_error("reload", device_commands) is None


def test_capability_error_is_a_noop_for_unknown_pre_handshake_device() -> None:
    """Deliberate: an unknown command set (device never reported one) must
    NEVER block dispatch -- otherwise `reload` itself could never reach a
    pre-Tier-0 device, breaking the automatic-update path for every
    currently-connected browser the moment this feature ships. See skew.py's
    module docstring, "Deliberately NOT a dispatch-blocking check for the
    unknown case"."""
    assert capability_error("reload", None) is None
    assert capability_error("some_command_that_does_not_exist", None) is None
