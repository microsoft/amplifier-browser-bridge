"""Tests for archive.py -- the browser-state archive orchestrator.

No real HubClient/websocket/browser is exercised here -- a fake `_ArchiveClient`
(same duck-typing pattern as test_vision.py's fake for vision_read.py) answers
`list_devices()`/`command()` from a canned script, and every assertion inspects
either the returned manifest or the files actually written under `tmp_path`.

Covers CONTRIBUTING.md's evidence-based testing convention for this feature:
depth-ladder composition, the no-wake guarantee, per-tab failure recording
without faking overall success, impossible-depth loud failure, and manifest
shape.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.archive import (
    _CDP_PACED_COMMANDS,
    DEFAULT_CDP_BURST_COOLDOWN_S,
    DEFAULT_CDP_BURST_SIZE,
    DEFAULT_CDP_CHUNK_COOLDOWN_S,
    DEFAULT_CDP_CHUNK_SIZE,
    DEFAULT_DEVICE_HEALTH_TRIP_THRESHOLD,
    DEFAULT_DEVICE_RECOVERY_MAX_WAIT_S,
    ArchiveError,
    _chunk_tabs,
    _is_device_disconnect_error,
    _is_per_tab_cdp_session_error,
    run_archive,
)
from amplifier_browser_bridge.client import HubError

# ---------------------------------------------------------------------------
# Fixtures: a fake device + a scriptable fake HubClient
# ---------------------------------------------------------------------------

_DEVICE_ID = "d1"


def _device_record(**capabilities: bool) -> dict[str, Any]:
    return {
        "device_id": _DEVICE_ID,
        "profile_id": "p1",
        "label": "edge-macos",
        "platform": "MacIntel",
        "capabilities": {
            "scripting": True,
            "windows": True,
            "tab_groups": True,
            "debugger": False,
            "capture_visible_tab": True,
            "downloads": True,
            "storage": True,
            "alarms": True,
            "history": True,
            "bookmarks": True,
            "sessions": True,
            "top_sites": True,
            "reading_list": True,
            "cookies": True,
            **capabilities,
        },
        "protocol_version": 1,
        "connected": True,
        "tier": "live",
        "last_seen": "2026-08-14T00:00:00+00:00",
        "queue_length": 0,
    }


def _tab(
    tab_id: int,
    *,
    window_id: int = 1,
    url: str = "https://example.com",
    title: str = "Test Page",
    discarded: bool = False,
    asleep: bool = False,
) -> dict[str, Any]:
    return {
        "tab_id": tab_id,
        "window_id": window_id,
        "url": url,
        "title": title,
        "active": False,
        "index": 0,
        "discarded": discarded,
        "asleep": asleep,
        "status": "unloaded" if (discarded or asleep) else "complete",
        "group_id": None,
        "fav_icon_url": None,
        "pinned": False,
        "audible": False,
        "muted": False,
        "mute_reason": None,
        "last_accessed": 1000,
    }


_WINDOWS_RESULT = {
    "ok": True,
    "result": {
        "windows": [
            {
                "window_id": 1,
                "focused": True,
                "state": "normal",
                "type": "normal",
                "incognito": False,
                "top": 0,
                "left": 0,
                "width": 1200,
                "height": 800,
            }
        ],
        "tab_groups": [
            {"group_id": 1, "window_id": 1, "title": "Group A", "color": "blue", "collapsed": False}
        ],
    },
}


class FakeArchiveClient:
    """Duck-typed `_ArchiveClient` -- scripted per-command responses, no
    network/websocket of any kind. `by_command` maps a command name to either
    a single canned response (returned for every call) or a list of
    responses (consumed in order, one per call -- for a command invoked once
    per tab).

    `devices` may also be a zero-arg callable, so a test can make `tier`
    change across successive `list_devices()` calls (needed to exercise
    `_CdpPacer`'s tier-reactive backpressure -- see the "Pacing" tests below).

    `poll_script` maps a `command_id` (the one the TEST itself embeds in a
    scripted `{"status": "queued", "command_id": ...}` response) to either a
    single canned poll response or a list of responses consumed in order --
    the same scripting convention as `by_command`. This is what lets a test
    prove a queued command resolves to a real result via a SUBSEQUENT poll
    (see test_queued_response_resolves_via_poll_and_lands_as_success below),
    rather than only ever exercising the immediate, `live`-device response.
    """

    def __init__(
        self,
        devices: list[dict[str, Any]] | Callable[[], list[dict[str, Any]]],
        by_command: dict[str, Any],
        *,
        poll_script: dict[str, Any] | None = None,
    ) -> None:
        self._devices = devices
        self._by_command = by_command
        self._poll_script = poll_script or {}
        self._call_index: dict[str, int] = {}
        self._poll_call_index: dict[str, int] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []  # (device_id, command, args)
        self.poll_calls: list[tuple[str, str]] = []  # (device_id, command_id)
        self.list_devices_calls: int = 0
        # Wall-clock timestamp of every `command()` call whose `args` marks it
        # as CDP (`_cdp_marker` -- set by the tests below, never a real wire
        # arg), for pacing-interval assertions.
        self.cdp_dispatch_times: list[float] = []

    async def list_devices(self) -> list[dict[str, Any]]:
        self.list_devices_calls += 1
        return self._devices() if callable(self._devices) else self._devices

    async def command(self, target: Target, command: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((target.device_id, command, dict(args)))
        if command in _CDP_PACED_COMMANDS or args.get("capture_hidden"):
            self.cdp_dispatch_times.append(time.monotonic())
        scripted = self._by_command.get(command, {"ok": True, "result": {}})
        if isinstance(scripted, list):
            idx = self._call_index.get(command, 0)
            self._call_index[command] = idx + 1
            scripted = scripted[idx] if idx < len(scripted) else scripted[-1]
        # A scripted exception instance (rather than a dict) simulates a
        # TRANSPORT-level failure -- e.g. HubError, the real HubClient's own
        # exception type for a connection-level failure (oversized payload,
        # timeout, refused connection, ...) -- as opposed to the hub-level
        # `{"ok": False, ...}` shape simulated above. Exercises `archive.py`'s
        # `_safe_command` choke point (see test_oversized_capture_failure_*
        # below), which is what turns an exception like this into an
        # ordinary per-capture failure instead of aborting the whole run.
        if isinstance(scripted, BaseException):
            raise scripted
        return scripted

    async def poll(self, device_id: str, command_id: str) -> dict[str, Any]:
        self.poll_calls.append((device_id, command_id))
        scripted = self._poll_script.get(command_id)
        if scripted is None:
            return {"ok": False, "error": f"no poll script for command_id={command_id!r}"}
        if isinstance(scripted, list):
            idx = self._poll_call_index.get(command_id, 0)
            self._poll_call_index[command_id] = idx + 1
            scripted = scripted[idx] if idx < len(scripted) else scripted[-1]
        if isinstance(scripted, BaseException):
            raise scripted
        return scripted


def _basic_client(
    *,
    tabs: list[dict[str, Any]] | None = None,
    capabilities: dict[str, bool] | None = None,
    extra_commands: dict[str, Any] | None = None,
    poll_script: dict[str, Any] | None = None,
    devices: Callable[[], list[dict[str, Any]]] | None = None,
) -> FakeArchiveClient:
    tabs = tabs if tabs is not None else [_tab(101), _tab(102)]
    by_command: dict[str, Any] = {
        "windows": _WINDOWS_RESULT,
        "tabs": {"ok": True, "result": tabs},
        "read": {
            "ok": True,
            "result": {"url": "https://example.com", "title": "Test Page", "text": "hello world"},
        },
        "page_state": {
            "ok": True,
            "result": {
                "url": "https://example.com",
                "title": "Test Page",
                "outer_html": "<html><body>hi</body></html>",
                "outer_html_chars": 29,
                "outer_html_truncated": False,
                "forms": [],
                "local_storage": {},
                "session_storage": {},
                "scroll": {"x": 0, "y": 0},
            },
        },
        "screenshot": {
            "ok": True,
            "result": {"tab_id": 101, "format": "jpg", "base64": "aGVsbG8=", "via": "captureVisibleTab"},
        },
        "mhtml": {"ok": True, "result": {"tab_id": 101, "format": "mhtml", "bytes": 5, "data": "MHTML-DATA"}},
        "nav_history": {
            "ok": True,
            "result": {
                "tab_id": 101,
                "current_index": 0,
                "entries": [{"id": 1, "url": "https://example.com"}],
            },
        },
        "history_list": {
            "ok": True,
            "result": {"entries": [{"url": "https://example.com", "title": "Test Page"}]},
        },
        "bookmarks_list": {"ok": True, "result": {"entries": [{"id": "1", "title": "Bar", "url": None}]}},
        "sessions_list": {"ok": True, "result": {"recently_closed": [], "devices": []}},
        "top_sites": {
            "ok": True,
            "result": {"entries": [{"url": "https://example.com", "title": "Test Page"}]},
        },
        "reading_list": {"ok": True, "result": {"entries": []}},
        "cookies_list": {"ok": True, "result": {"entries": [{"name": "session", "value": "secret-token"}]}},
    }
    if extra_commands:
        by_command.update(extra_commands)
    device_source: Callable[[], list[dict[str, Any]]] | list[dict[str, Any]]
    device_source = devices if devices is not None else [_device_record(**(capabilities or {}))]
    return FakeArchiveClient(device_source, by_command, poll_script=poll_script)


# ---------------------------------------------------------------------------
# Depth ladder composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l0_does_not_contact_any_tab(tmp_path: Path) -> None:
    """L0 must work with zero tab contact -- no read/page_state/screenshot/
    mhtml/nav_history call for any tab, even though tabs exist."""
    client = _basic_client()
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0")

    assert result["ok"] is True
    manifest = result["result"]
    assert manifest["tabs"] == {}
    per_tab_commands = {"read", "page_state", "screenshot", "mhtml", "nav_history"}
    assert not any(cmd in per_tab_commands for (_dev, cmd, _args) in client.calls)
    # Inventory-only commands DID run.
    assert any(cmd == "windows" for (_dev, cmd, _args) in client.calls)
    assert any(cmd == "tabs" for (_dev, cmd, _args) in client.calls)


@pytest.mark.asyncio
async def test_l1_adds_text_only(tmp_path: Path) -> None:
    client = _basic_client()
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]
    for tab_entry in manifest["tabs"].values():
        assert set(tab_entry["captures"]) == {"text"}


@pytest.mark.asyncio
async def test_l2_adds_dom_on_top_of_text(tmp_path: Path) -> None:
    client = _basic_client()
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L2")
    manifest = result["result"]
    for tab_entry in manifest["tabs"].values():
        assert set(tab_entry["captures"]) == {"text", "dom"}


@pytest.mark.asyncio
async def test_l3_adds_screenshots(tmp_path: Path) -> None:
    client = _basic_client()
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L3")
    manifest = result["result"]
    for tab_entry in manifest["tabs"].values():
        assert set(tab_entry["captures"]) == {"text", "dom", "screenshot"}


@pytest.mark.asyncio
async def test_l4_adds_mhtml_and_requires_debugger(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L4")
    manifest = result["result"]
    for tab_entry in manifest["tabs"].values():
        assert set(tab_entry["captures"]) == {"text", "dom", "screenshot", "mhtml"}


@pytest.mark.asyncio
async def test_l5_adds_nav_history_and_profile_data(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]
    for tab_entry in manifest["tabs"].values():
        assert set(tab_entry["captures"]) == {"text", "dom", "screenshot", "mhtml", "nav_history"}
    assert manifest["profile"] is not None
    assert set(manifest["profile"]) == {
        "history",
        "bookmarks",
        "sessions",
        "top_sites",
        "reading_list",
        "cookies",
    }


@pytest.mark.asyncio
async def test_each_level_is_a_strict_superset_of_the_previous(tmp_path: Path) -> None:
    """The depth ladder's own contract: L(n+1)'s per-tab capture keys must be a
    strict superset of L(n)'s."""
    prior_keys: set[str] = set()
    for i, depth in enumerate(("L0", "L1", "L2", "L3", "L4", "L5")):
        client = _basic_client(capabilities={"debugger": True})
        result = await run_archive(client, _DEVICE_ID, tmp_path / depth, depth=depth)
        manifest = result["result"]
        if manifest["tabs"]:
            keys = set(next(iter(manifest["tabs"].values()))["captures"])
        else:
            keys = set()
        assert prior_keys <= keys, f"{depth} lost capture(s) present at a lower depth: {prior_keys - keys}"
        prior_keys = keys
        del i  # only used for readability of the loop


# ---------------------------------------------------------------------------
# No-wake guarantee
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discarded_tab_is_skipped_not_captured_by_default(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101, discarded=True)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L2")
    manifest = result["result"]
    entry = manifest["tabs"]["101"]
    assert entry["status"] == "skipped"
    assert "wake=True" in entry["reason"]
    assert entry["captures"] == {}
    # No per-tab command was ever issued for this tab.
    per_tab_calls = [c for c in client.calls if c[0] == _DEVICE_ID and c[1] in ("read", "page_state")]
    assert per_tab_calls == []


@pytest.mark.asyncio
async def test_asleep_tab_is_also_skipped_by_default(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101, asleep=True)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    entry = result["result"]["tabs"]["101"]
    assert entry["status"] == "skipped"


@pytest.mark.asyncio
async def test_wake_true_allows_capturing_a_discarded_tab(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101, discarded=True)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", wake=True)
    entry = result["result"]["tabs"]["101"]
    assert entry["status"] == "ok"
    assert "text" in entry["captures"]
    # The wake=True opt-in was actually forwarded to the wire command.
    read_calls = [c for c in client.calls if c[1] == "read"]
    assert read_calls and read_calls[0][2].get("wake") is True


@pytest.mark.asyncio
async def test_non_discarded_tabs_never_receive_a_wake_arg_by_default(tmp_path: Path) -> None:
    """Even for AWAKE tabs, the orchestrator must never pass wake=true unless
    the caller explicitly opted in -- this is the general guarantee, not just
    the discarded-tab special case."""
    client = _basic_client(tabs=[_tab(101, discarded=False, asleep=False)])
    await run_archive(client, _DEVICE_ID, tmp_path, depth="L2")
    for _dev, _cmd, args in client.calls:
        assert "wake" not in args


@pytest.mark.asyncio
async def test_archive_never_passes_activate(tmp_path: Path) -> None:
    """The archive orchestrator must never steal focus -- no call, at any
    depth, ever carries an `activate` arg."""
    client = _basic_client(capabilities={"debugger": True})
    await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    for _dev, _cmd, args in client.calls:
        assert "activate" not in args


# ---------------------------------------------------------------------------
# Per-tab failure recorded, run continues, overall result is honest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_tab_failing_does_not_abort_the_run(tmp_path: Path) -> None:
    client = _basic_client(
        tabs=[_tab(101), _tab(102)],
        extra_commands={
            "read": [
                {"ok": True, "result": {"url": "https://example.com", "title": "Test Page", "text": "hello"}},
                {"ok": False, "error": "tab 102 is discarded: ..."},
            ]
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]
    assert manifest["tabs"]["101"]["status"] == "ok"
    assert manifest["tabs"]["102"]["status"] == "failed"
    assert manifest["tabs"]["102"]["captures"]["text"]["status"] == "failed"


@pytest.mark.asyncio
async def test_failures_are_recorded_at_the_top_level_and_never_buried(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101)], extra_commands={"read": {"ok": False, "error": "boom"}})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]
    assert len(manifest["failures"]) == 1
    assert manifest["failures"][0]["scope"] == "tab"
    assert manifest["failures"][0]["tab_id"] == 101
    assert manifest["failures"][0]["capture"] == "text"
    assert manifest["failures"][0]["error"] == "boom"


# ---------------------------------------------------------------------------
# A TRANSPORT-level failure (HubError -- e.g. an oversized MHTML payload
# tripping the client's websocket size cap, or any other connection-level
# failure) on one tab's capture must fail only that capture, never abort the
# whole archive run. Real-world finding: archiving four real web pages at
# MHTML depth (L4) raised HubError straight out of `client.command()`,
# uncaught, killing the entire run partway through -- every tab after the
# pathological one was silently lost, and `manifest.json` (written only once,
# at the very end of `run_archive`) was never written at all. See client.py's
# module docstring and protocol.py's "WebSocket message-size ceiling" section
# for the transport-level half of this fix; this is the orchestrator-level
# half (`archive.py`'s `_safe_command`).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_huberror_on_one_tabs_mhtml_capture_does_not_abort_the_run(tmp_path: Path) -> None:
    """Tab 101's `mhtml` capture raises `HubError` (simulating an oversized
    payload tripping the client's own websocket size cap, mid-archive) --
    this must be recorded as a failed capture for THAT tab, and the run must
    still reach tab 102 and complete, writing a real manifest.json to disk
    (never reached at all, prior to this fix, once the exception escaped
    uncaught)."""
    client = _basic_client(
        tabs=[_tab(101), _tab(102)],
        capabilities={"debugger": True},
        extra_commands={
            "mhtml": [
                HubError(
                    "could not reach hub at ws://100.124.126.19:8900/agent: sent 1009 (message too "
                    "big) frame exceeds limit of 1048576 bytes; no close frame received"
                ),
                {"ok": True, "result": {"tab_id": 102, "format": "mhtml", "bytes": 5, "data": "MHTML-DATA"}},
            ]
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L4")
    manifest = result["result"]

    # The pathological tab: capture failed, not an uncaught exception.
    assert manifest["tabs"]["101"]["captures"]["mhtml"]["status"] == "failed"
    assert "message too big" in manifest["tabs"]["101"]["captures"]["mhtml"]["error"]

    # The run continued: tab 102 was reached and fully captured.
    assert manifest["tabs"]["102"]["status"] == "ok"
    assert manifest["tabs"]["102"]["captures"]["mhtml"]["status"] == "ok"

    # The run reached completion at all -- manifest.json actually exists.
    manifest_path = Path(manifest["archive_dir"]) / "manifest.json"
    assert manifest_path.exists()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "ok_with_failures"


@pytest.mark.asyncio
async def test_huberror_on_top_level_windows_or_tabs_inventory_does_not_raise(tmp_path: Path) -> None:
    """The same transport-level resilience applies to the top-level
    `windows`/`tabs` inventory calls, not just per-tab captures -- a HubError
    there must not raise out of `run_archive` either."""
    client = _basic_client(extra_commands={"windows": HubError("could not reach hub: connection refused")})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0")
    manifest = result["result"]
    assert manifest["windows"]["status"] == "failed"
    assert "connection refused" in manifest["windows"]["error"]
    assert manifest["status"] == "ok_with_failures"


@pytest.mark.asyncio
async def test_overall_status_is_never_plain_ok_when_a_tab_failed(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101)], extra_commands={"read": {"ok": False, "error": "boom"}})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]
    assert manifest["status"] == "ok_with_failures"
    assert manifest["summary"]["has_failures"] is True
    assert manifest["summary"]["tabs_failed"] == 1


# ---------------------------------------------------------------------------
# Injection budget and explicit capture selection (module docstring's
# "Injection budget and explicit capture selection" section) -- the fix for
# the depth ladder making deep captures unaffordable: JS-injection captures
# (text/dom) can time out on heavy hydrated SPAs while CDP-based captures on
# the same tab succeed in seconds. `injection_timeout_s` bounds the wait
# without ever skipping; `captures` lets a caller skip outright.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injection_timeout_s_overrides_timeout_for_read_and_page_state_only(tmp_path: Path) -> None:
    """`injection_timeout_s` must reach ONLY `read`/`page_state`'s `timeout_s`
    arg -- `screenshot`/`mhtml`/`nav_history` keep using the general
    `timeout_s`, unchanged."""
    client = _basic_client(capabilities={"debugger": True})
    await run_archive(client, _DEVICE_ID, tmp_path, depth="L5", timeout_s=120.0, injection_timeout_s=15.0)
    calls_by_command = {cmd: args for _dev, cmd, args in client.calls}
    assert calls_by_command["read"]["timeout_s"] == 15.0
    assert calls_by_command["page_state"]["timeout_s"] == 15.0
    assert calls_by_command["screenshot"]["timeout_s"] == 120.0
    assert calls_by_command["mhtml"]["timeout_s"] == 120.0
    assert calls_by_command["nav_history"]["timeout_s"] == 120.0


@pytest.mark.asyncio
async def test_injection_timeout_s_omitted_means_unchanged_behavior(tmp_path: Path) -> None:
    """Omitting `injection_timeout_s` (the default, `None`) must produce
    BYTE-FOR-BYTE the same args as before this feature existed -- `timeout_s`
    applies uniformly to every capture."""
    client = _basic_client(capabilities={"debugger": True})
    await run_archive(client, _DEVICE_ID, tmp_path, depth="L5", timeout_s=42.0)
    for _dev, cmd, args in client.calls:
        if cmd in ("read", "page_state", "screenshot", "mhtml", "nav_history"):
            assert args["timeout_s"] == 42.0


@pytest.mark.asyncio
async def test_captures_narrows_to_a_cdp_only_archive(tmp_path: Path) -> None:
    """`captures` excluding `text`/`dom` must mean `read`/`page_state` are
    NEVER CALLED AT ALL -- zero wall-clock cost, not attempted-then-bounded --
    while `screenshot`/`mhtml`/`nav_history` still run normally."""
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L4", captures=["mhtml", "screenshot"])
    manifest = result["result"]
    entry = manifest["tabs"]["101"]

    called_commands = {cmd for _dev, cmd, _args in client.calls}
    assert "read" not in called_commands
    assert "page_state" not in called_commands
    assert "screenshot" in called_commands
    assert "mhtml" in called_commands

    assert entry["captures"]["text"]["status"] == "skipped"
    assert "captures" in entry["captures"]["text"]["reason"]
    assert entry["captures"]["dom"]["status"] == "skipped"
    assert entry["captures"]["screenshot"]["status"] == "ok"
    assert entry["captures"]["mhtml"]["status"] == "ok"


@pytest.mark.asyncio
async def test_captures_config_skip_does_not_count_as_a_failure_or_partial(tmp_path: Path) -> None:
    """A tab where every ATTEMPTED capture succeeded must still report plain
    `"ok"` even though `captures` configured some captures out entirely --
    a config-narrowed request that fully succeeds is not a degraded run."""
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L4", captures=["mhtml", "screenshot"])
    manifest = result["result"]
    assert manifest["tabs"]["101"]["status"] == "ok"
    assert manifest["status"] == "ok"
    assert manifest["failures"] == []


@pytest.mark.asyncio
async def test_captures_requested_is_recorded_in_the_manifest(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})

    result_default = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    assert result_default["result"]["captures_requested"] is None

    result_narrowed = await run_archive(
        client, _DEVICE_ID, tmp_path, depth="L4", captures=["mhtml", "screenshot"]
    )
    assert result_narrowed["result"]["captures_requested"] == ["mhtml", "screenshot"]


@pytest.mark.asyncio
async def test_captures_empty_list_raises_before_any_capture(tmp_path: Path) -> None:
    client = _basic_client()
    with pytest.raises(ArchiveError, match="at least one capture"):
        await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", captures=[])
    assert client.calls == []


@pytest.mark.asyncio
async def test_captures_unknown_name_raises_before_any_capture(tmp_path: Path) -> None:
    client = _basic_client()
    with pytest.raises(ArchiveError, match="unrecognized capture"):
        await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", captures=["outer_html"])
    assert client.calls == []


@pytest.mark.asyncio
async def test_captures_unreachable_at_depth_raises_before_any_capture(tmp_path: Path) -> None:
    """`captures=["mhtml"]` at `depth="L1"` would silently capture NOTHING for
    every tab in the run (the ladder never reaches `mhtml` at L1) -- this
    must fail loud, pre-flight, exactly like an impossible depth."""
    client = _basic_client()
    with pytest.raises(ArchiveError, match="no capture reachable at depth"):
        await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", captures=["mhtml"])
    assert client.calls == []


@pytest.mark.asyncio
async def test_overall_status_is_ok_with_skips_when_only_skips_occurred(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101, discarded=True)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]
    assert manifest["status"] == "ok_with_skips"
    assert manifest["summary"]["has_failures"] is False
    assert manifest["summary"]["tabs_skipped"] == 1


@pytest.mark.asyncio
async def test_overall_status_is_plain_ok_when_everything_succeeds(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L2")
    manifest = result["result"]
    assert manifest["status"] == "ok"
    assert manifest["summary"] == {
        "windows_inventoried": 1,
        "tab_groups_inventoried": 1,
        "tabs_inventoried": 1,
        "tabs_capture_attempted": 1,
        "tabs_captured": 1,
        "tabs_partial": 0,
        "tabs_skipped": 0,
        "tabs_failed": 0,
        "tabs_not_found": 0,
        "profile": None,
        "has_failures": False,
    }


# ---------------------------------------------------------------------------
# Per-tab status is not binary: "partial" is the middle state between "ok"
# and "failed" -- the bug ff5bc16 fixed one level up (run-level summary),
# missed one level down (per-tab status).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tab_with_mixed_success_and_failure_reports_partial_not_ok_or_failed(
    tmp_path: Path,
) -> None:
    """The bug, observed live against a real tab whose page was a browser error
    page: `text`/`dom` (JS injection) failed outright ("Frame with ID 0 is
    showing error page") while `mhtml`/`screenshot`/`nav_history` (CDP-based)
    all succeeded, landing ~147KB of real artifacts on disk. That tab is
    neither a clean success nor a total loss -- it must report `"partial"`,
    and `summary` must count it as neither a captured tab nor a failed one."""
    client = _basic_client(
        tabs=[_tab(101)],
        capabilities={"debugger": True},
        extra_commands={
            "read": {"ok": False, "error": "Frame with ID 0 is showing error page"},
            "page_state": {"ok": False, "error": "Frame with ID 0 is showing error page"},
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]
    entry = manifest["tabs"]["101"]

    assert entry["status"] == "partial"
    assert entry["captures"]["text"]["status"] == "failed"
    assert entry["captures"]["dom"]["status"] == "failed"
    assert entry["captures"]["screenshot"]["status"] == "ok"
    assert entry["captures"]["mhtml"]["status"] == "ok"
    assert entry["captures"]["nav_history"]["status"] == "ok"

    # The load-bearing assertions: not a clean capture, not a total failure.
    assert manifest["summary"]["tabs_captured"] == 0
    assert manifest["summary"]["tabs_failed"] == 0
    assert manifest["summary"]["tabs_partial"] == 1


@pytest.mark.asyncio
async def test_tab_with_every_capture_failing_still_reports_failed(tmp_path: Path) -> None:
    """The other end of the same axis: a tab where NOTHING succeeded must still
    report `"failed"`, not `"partial"` -- `"partial"` is reserved for the
    genuine middle ground, not a synonym for any failure at all."""
    client = _basic_client(
        tabs=[_tab(101)],
        capabilities={"debugger": True},
        extra_commands={
            "read": {"ok": False, "error": "boom-text"},
            "page_state": {"ok": False, "error": "boom-dom"},
            "screenshot": {"ok": False, "error": "boom-screenshot"},
            "mhtml": {"ok": False, "error": "boom-mhtml"},
            "nav_history": {"ok": False, "error": "boom-nav"},
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]
    entry = manifest["tabs"]["101"]

    assert entry["status"] == "failed"
    assert manifest["summary"]["tabs_failed"] == 1
    assert manifest["summary"]["tabs_partial"] == 0
    assert manifest["summary"]["tabs_captured"] == 0


@pytest.mark.asyncio
async def test_skipped_tab_is_distinct_from_ok_partial_and_failed_tabs(tmp_path: Path) -> None:
    """A tab intentionally skipped (no-wake guarantee) is a FOURTH state, never
    confused with any of the three capture outcomes -- exercised here
    alongside a fully-ok tab and a fully-failed tab in the same run."""
    client = _basic_client(
        tabs=[_tab(101, discarded=True), _tab(102), _tab(103)],
        extra_commands={
            "read": [
                {"ok": True, "result": {"url": "https://example.com", "title": "Test Page", "text": "hi"}},
                {"ok": False, "error": "boom"},
            ],
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]

    assert manifest["tabs"]["101"]["status"] == "skipped"
    assert manifest["tabs"]["102"]["status"] == "ok"
    assert manifest["tabs"]["103"]["status"] == "failed"

    summary = manifest["summary"]
    assert summary["tabs_skipped"] == 1
    assert summary["tabs_captured"] == 1
    assert summary["tabs_failed"] == 1
    assert summary["tabs_partial"] == 0


@pytest.mark.asyncio
async def test_run_level_status_is_not_plain_ok_when_a_tab_is_partial(tmp_path: Path) -> None:
    """A run containing a partial tab must not be reported as plain `"ok"` --
    the per-tab capture failures underlying the partial status always land in
    the top-level `failures` list (via `_capture_tab`'s `record`), which forces
    the already-existing degraded-status branch. Verified explicitly rather
    than assumed, since this is the caller-visible guarantee from the bug
    report ("keep manifest['status'] honest")."""
    client = _basic_client(
        tabs=[_tab(101)],
        capabilities={"debugger": True},
        extra_commands={"read": {"ok": False, "error": "boom-text"}},
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]

    assert manifest["tabs"]["101"]["status"] == "partial"
    assert manifest["status"] == "ok_with_failures"
    assert manifest["status"] != "ok"


@pytest.mark.asyncio
async def test_partial_tab_failures_still_carry_original_error_text(tmp_path: Path) -> None:
    """`manifest["failures"]` is what told the reporter the real cause of the
    live bug ("Frame with ID 0 is showing error page") -- it must not get
    quieter just because the tab's rolled-up status is now `"partial"` instead
    of `"failed"`."""
    client = _basic_client(
        tabs=[_tab(101)],
        capabilities={"debugger": True},
        extra_commands={
            "read": {"ok": False, "error": "Frame with ID 0 is showing error page"},
            "page_state": {"ok": False, "error": "Frame with ID 0 is showing error page"},
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]

    assert manifest["tabs"]["101"]["status"] == "partial"
    text_failures = [f for f in manifest["failures"] if f["capture"] == "text"]
    dom_failures = [f for f in manifest["failures"] if f["capture"] == "dom"]
    assert len(text_failures) == 1
    assert text_failures[0]["error"] == "Frame with ID 0 is showing error page"
    assert len(dom_failures) == 1
    assert dom_failures[0]["error"] == "Frame with ID 0 is showing error page"


# ---------------------------------------------------------------------------
# Manifest-accuracy bug: summary must never read as "nothing was archived"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l0_summary_reports_tabs_inventoried_not_zero(tmp_path: Path) -> None:
    """The bug, observed live against a real 735-tab profile: an L0 archive
    wrote every tab to disk successfully, but `summary["tabs_total"]` (computed
    from the empty per-tab capture manifest) reported `0`, and there was no
    other unmissable field in `summary` saying otherwise. `tabs_inventoried`
    must report the real count at L0 -- the depth that, by design, captures
    zero page content."""
    n = 735
    tabs = [_tab(100 + i) for i in range(n)]
    client = _basic_client(tabs=tabs)
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0")
    manifest = result["result"]

    assert manifest["status"] == "ok"
    assert manifest["tabs_inventory"]["count"] == n
    # The load-bearing assertion: `summary` itself -- not just a sibling key --
    # must make the real count unmissable, and must never claim zero.
    assert manifest["summary"]["tabs_inventoried"] == n
    assert manifest["summary"]["tabs_inventoried"] != 0
    # L0 legitimately captures no page content -- that is success, not failure,
    # and must remain distinguishable from the inventory count above.
    assert manifest["summary"]["tabs_capture_attempted"] == 0
    assert manifest["summary"]["tabs_captured"] == 0
    assert manifest["summary"]["tabs_skipped"] == 0
    assert manifest["summary"]["tabs_failed"] == 0
    assert manifest["summary"]["has_failures"] is False


@pytest.mark.asyncio
async def test_l0_summary_also_reports_windows_and_tab_groups_inventoried(tmp_path: Path) -> None:
    """The same under-reporting bug could equally hit the windows/tab-groups
    axes -- both are captured outside `tab_manifest` too."""
    client = _basic_client()
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0")
    manifest = result["result"]
    assert manifest["summary"]["windows_inventoried"] == manifest["windows"]["count"] == 1
    assert manifest["summary"]["tab_groups_inventoried"] == manifest["tab_groups"]["count"] == 1


@pytest.mark.asyncio
async def test_deeper_depth_still_distinguishes_inventoried_from_captured(tmp_path: Path) -> None:
    """At L2 with every tab capturing successfully, `tabs_inventoried` and
    `tabs_captured` happen to be equal in VALUE -- but they must remain
    distinct KEYS reporting distinct axes (inventory vs. content capture),
    never collapsed into a single number."""
    tabs = [_tab(101), _tab(102), _tab(103)]
    client = _basic_client(tabs=tabs)
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L2")
    summary = result["result"]["summary"]
    assert summary["tabs_inventoried"] == 3
    assert summary["tabs_capture_attempted"] == 3
    assert summary["tabs_captured"] == 3
    # Distinct keys must both be present and correct, not merged into one.
    assert "tabs_inventoried" in summary
    assert "tabs_capture_attempted" in summary


@pytest.mark.asyncio
async def test_tabs_inventoried_is_full_count_even_when_tab_ids_narrows_capture(
    tmp_path: Path,
) -> None:
    """`tab_ids` restricts L1+ per-tab CAPTURE to a subset, but the INVENTORY
    axis must always report the true total -- never silently narrowed to
    match the capture subset."""
    tabs = [_tab(101), _tab(102), _tab(103)]
    client = _basic_client(tabs=tabs)
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", tab_ids=[101])
    summary = result["result"]["summary"]
    assert summary["tabs_inventoried"] == 3
    assert summary["tabs_capture_attempted"] == 1
    assert summary["tabs_captured"] == 1


@pytest.mark.asyncio
async def test_windows_inventoried_is_none_not_zero_when_windows_capture_fails(
    tmp_path: Path,
) -> None:
    """A failed `windows` capture must report `None` (unknown), never a bare
    `0` that could be misread as "zero windows exist" -- and must still push
    the overall run to a degraded status, never plain `"ok"`."""
    client = _basic_client(extra_commands={"windows": {"ok": False, "error": "boom"}})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0")
    manifest = result["result"]
    assert manifest["status"] == "ok_with_failures"
    assert manifest["summary"]["has_failures"] is True
    assert manifest["summary"]["windows_inventoried"] is None
    assert manifest["summary"]["tab_groups_inventoried"] is None
    # The inventory failure is not silently absorbed -- it is a real, named
    # top-level failure.
    assert any(f["scope"] == "windows" for f in manifest["failures"])


@pytest.mark.asyncio
async def test_tabs_inventoried_is_none_not_zero_when_tabs_capture_fails(tmp_path: Path) -> None:
    client = _basic_client(extra_commands={"tabs": {"ok": False, "error": "boom"}})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0")
    manifest = result["result"]
    assert manifest["status"] == "ok_with_failures"
    assert manifest["summary"]["tabs_inventoried"] is None
    assert any(f["scope"] == "tabs_inventory" for f in manifest["failures"])


@pytest.mark.asyncio
async def test_profile_summary_is_none_below_l5_and_populated_at_l5(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})
    below = await run_archive(client, _DEVICE_ID, tmp_path / "L2", depth="L2")
    assert below["result"]["summary"]["profile"] is None

    at_l5 = await run_archive(client, _DEVICE_ID, tmp_path / "L5", depth="L5")
    profile_summary = at_l5["result"]["summary"]["profile"]
    assert profile_summary is not None
    # 5 _PROFILE_SPECS items + cookies (skipped by default, opt-in) == 6.
    assert profile_summary["items_total"] == 6
    assert profile_summary["items_captured"] == 5
    assert profile_summary["items_skipped"] == 1  # cookies, intentional opt-out
    assert profile_summary["items_failed"] == 0
    # The intentional cookies opt-out is not a failure and must not push
    # overall status away from plain "ok".
    assert at_l5["result"]["status"] == "ok"


@pytest.mark.asyncio
async def test_profile_summary_counts_a_real_profile_item_failure(tmp_path: Path) -> None:
    client = _basic_client(
        capabilities={"debugger": True},
        extra_commands={"history_list": {"ok": False, "error": "boom"}},
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]
    profile_summary = manifest["summary"]["profile"]
    assert profile_summary is not None
    assert profile_summary["items_failed"] == 1
    assert profile_summary["items_captured"] == 4
    assert manifest["status"] == "ok_with_failures"
    assert manifest["summary"]["has_failures"] is True


@pytest.mark.asyncio
async def test_queued_response_resolves_via_poll_and_lands_as_success(tmp_path: Path) -> None:
    """The load-bearing regression test for the real-world bug: a device that
    is not live returns {"status": "queued", ...} immediately, but the hub
    goes on to actually execute the command and the real result becomes
    retrievable via `poll`. This capture must land in the manifest as a
    SUCCESS (the work the device actually did), not a failure -- the prior
    behavior (treating `queued` itself as the final, failed outcome) is
    exactly the bug that discarded ~90 real captures in the live 126-tab
    archive this fix responds to. A test where every command returns
    immediately (the OLD version of this test) cannot catch this bug."""
    client = _basic_client(
        tabs=[_tab(101)],
        extra_commands={
            "read": {"status": "queued", "command_id": "c1", "tier": "intermittent", "queue_position": 1}
        },
        poll_script={
            "c1": [
                {"status": "queued", "queue_position": 1, "tier": "intermittent"},
                {"status": "pending"},
                {
                    "ok": True,
                    "result": {"url": "https://example.com", "title": "Test Page", "text": "hello"},
                },
            ]
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", poll_interval_s=0.01)
    entry = result["result"]["tabs"]["101"]
    assert entry["status"] == "ok"
    assert entry["captures"]["text"]["status"] == "ok"
    # The poll mechanism was actually exercised -- not a coincidental pass.
    assert client.poll_calls == [(_DEVICE_ID, "c1"), (_DEVICE_ID, "c1"), (_DEVICE_ID, "c1")]
    manifest = result["result"]
    assert manifest["status"] == "ok"
    assert manifest["pacing"]["queued_waits"] == 1
    assert manifest["pacing"]["queued_timeouts"] == 0


@pytest.mark.asyncio
async def test_a_realistic_fraction_of_commands_queued_all_land_as_successes(tmp_path: Path) -> None:
    """Multiple tabs, each queued at least once, each resolving via poll to a
    real success -- proving the fix holds across a whole run, not just one
    lucky capture."""
    tabs = [_tab(101), _tab(102), _tab(103)]

    def _queued(command_id: str) -> dict[str, Any]:
        return {"status": "queued", "command_id": command_id, "tier": "intermittent"}

    client = _basic_client(
        tabs=tabs,
        extra_commands={"read": [_queued("c-101"), _queued("c-102"), _queued("c-103")]},
        poll_script={
            "c-101": {"ok": True, "result": {"url": "https://example.com", "title": "t", "text": "a"}},
            "c-102": [
                {"status": "pending"},
                {"ok": True, "result": {"url": "https://example.com", "title": "t", "text": "b"}},
            ],
            "c-103": {"ok": False, "error": "device reconnected but the tab was closed"},
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", poll_interval_s=0.01)
    manifest = result["result"]
    assert manifest["tabs"]["101"]["status"] == "ok"
    assert manifest["tabs"]["102"]["status"] == "ok"
    # 103's queued command genuinely resolved to a real failure -- this must
    # still be reported honestly as failed, never miscounted as a success.
    assert manifest["tabs"]["103"]["status"] == "failed"
    assert manifest["pacing"]["queued_waits"] == 3


@pytest.mark.asyncio
async def test_queued_command_that_never_resolves_fails_loud_not_hangs_forever(tmp_path: Path) -> None:
    """A queued command whose poll NEVER resolves (always "queued"/"pending")
    must not hang the archive indefinitely -- it must give up after
    `poll_max_wait_s` and record an honest failure. Uses tiny poll_interval_s/
    poll_max_wait_s so the test itself completes quickly while still proving
    the timeout path (not just asserting behavior by inspection)."""
    client = _basic_client(
        tabs=[_tab(101)],
        extra_commands={"read": {"status": "queued", "command_id": "stuck", "tier": "dormant"}},
        poll_script={"stuck": {"status": "queued", "queue_position": 1, "tier": "dormant"}},
    )
    started = time.monotonic()
    result = await run_archive(
        client, _DEVICE_ID, tmp_path, depth="L1", poll_interval_s=0.01, poll_max_wait_s=0.05
    )
    elapsed = time.monotonic() - started
    # The load-bearing assertion: this returned at all, and quickly -- a bug
    # that polled forever would hang this test (and the real archive) rather
    # than ever reaching this line.
    assert elapsed < 5.0
    entry = result["result"]["tabs"]["101"]
    assert entry["status"] == "failed"
    assert "gave up waiting" in entry["captures"]["text"]["error"]
    assert result["result"]["pacing"]["queued_timeouts"] == 1


@pytest.mark.asyncio
async def test_on_progress_receives_queued_wait_events(tmp_path: Path) -> None:
    """`on_progress` (module docstring's "Pacing" section) is a cheap, live
    signal of real activity -- a small sample never saturates anything, so a
    caller archiving hundreds of tabs should be able to see queued-wait
    activity as it happens, not only in the final manifest."""
    client = _basic_client(
        tabs=[_tab(101)],
        extra_commands={"read": {"status": "queued", "command_id": "c1", "tier": "intermittent"}},
        poll_script={"c1": {"ok": True, "result": {"url": "https://example.com", "title": "t", "text": "x"}}},
    )
    events: list[dict[str, Any]] = []
    await run_archive(
        client, _DEVICE_ID, tmp_path, depth="L1", poll_interval_s=0.01, on_progress=events.append
    )
    event_names = [e["event"] for e in events]
    assert "queued_wait_started" in event_names
    assert "queued_wait_resolved" in event_names
    assert "tab_done" in event_names
    assert "archive_finished" in event_names


# ---------------------------------------------------------------------------
# Pacing -- CDP dispatches must never fire back-to-back with zero gap, and
# must back off when the device shows signs of degrading. See module
# docstring's "Pacing" section for the real-world incident (a 126-tab archive
# firing ~378 CDP-heavy commands as fast as it could, knocking the device off
# `live` mid-run) this closes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdp_pace_s_bounds_the_dispatch_rate_of_cdp_commands(tmp_path: Path) -> None:
    """The load-bearing pacing assertion: successive CDP-requiring dispatches
    (`mhtml`, here) must be spaced at least `cdp_pace_s` apart -- proving the
    pacer actually throttles the rate at which commands reach the device,
    not just that it exists. Three tabs -> three `mhtml` dispatches -> two
    enforced gaps."""
    client = _basic_client(
        tabs=[_tab(101), _tab(102), _tab(103)],
        capabilities={"debugger": True},
    )
    await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.05,
        cdp_backpressure_max_wait_s=0,
    )
    assert len(client.cdp_dispatch_times) == 3
    gaps = [b - a for a, b in zip(client.cdp_dispatch_times, client.cdp_dispatch_times[1:])]
    for gap in gaps:
        assert gap >= 0.045, f"CDP dispatches were not paced apart: gap={gap:.4f}s"


@pytest.mark.asyncio
async def test_cdp_pace_s_zero_disables_the_floor(tmp_path: Path) -> None:
    """`cdp_pace_s=0` (opt-out) must not add any artificial delay -- confirms
    the floor is a real, disableable knob, not a hardcoded minimum."""
    client = _basic_client(tabs=[_tab(101), _tab(102)], capabilities={"debugger": True})
    started = time.monotonic()
    await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0,
        cdp_backpressure_max_wait_s=0,
    )
    assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_only_cdp_commands_are_paced_not_injection_or_profile_commands(tmp_path: Path) -> None:
    """Lightweight JS-injection captures (`text`/`dom`) and browser-wide
    profile-data commands must never be paced -- only `mhtml`/`nav_history`/
    `screenshot` (capture_hidden) are, per module docstring's "Pacing"
    section. A large `cdp_pace_s` here would make this test slow if pacing
    leaked onto the wrong commands."""
    client = _basic_client(tabs=[_tab(101), _tab(102), _tab(103)], capabilities={"debugger": True})
    started = time.monotonic()
    await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L2",
        cdp_pace_s=5.0,
        cdp_backpressure_max_wait_s=0,
    )
    assert time.monotonic() - started < 2.0
    assert client.cdp_dispatch_times == []


@pytest.mark.asyncio
async def test_backpressure_pauses_cdp_dispatch_until_tier_recovers(tmp_path: Path) -> None:
    """The device reports `intermittent` for the first two tier checks, then
    recovers to `live` -- the pacer must wait (not proceed immediately) and
    must resume once tier reports `live` again, rather than either blocking
    forever or ignoring the degraded tier entirely.

    The first `"intermittent"` is consumed by `run_archive`'s OWN up-front
    `list_devices()` call (to resolve capabilities, before any dispatch or
    pacing happens at all) -- the second is what the pacer itself actually
    observes on its first tier check."""
    tier_sequence = iter(["intermittent", "intermittent", "live", "live", "live"])

    def _devices() -> list[dict[str, Any]]:
        record = _device_record(debugger=True)
        record["tier"] = next(tier_sequence, "live")
        return [record]

    client = _basic_client(tabs=[_tab(101)], capabilities={"debugger": True}, devices=_devices)
    events: list[dict[str, Any]] = []
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=5.0,
        on_progress=events.append,
    )
    assert result["result"]["tabs"]["101"]["status"] == "ok"
    event_names = [e["event"] for e in events]
    assert "backpressure_waiting" in event_names
    assert "backpressure_resumed" in event_names
    assert result["result"]["pacing"]["cdp_backpressure_events"] == 1


@pytest.mark.asyncio
async def test_backpressure_gives_up_after_max_wait_and_dispatches_anyway(tmp_path: Path) -> None:
    """A device stuck non-live for longer than `cdp_backpressure_max_wait_s`
    must not block the archive forever -- the pacer gives up and lets
    dispatch proceed (whatever happens next, e.g. a queued response, is
    `_resolve_queued`'s job to handle correctly, not the pacer's)."""
    client = _basic_client(
        tabs=[_tab(101)],
        capabilities={"debugger": True},
        devices=lambda: [{**_device_record(debugger=True), "tier": "dormant"}],
    )
    started = time.monotonic()
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.05,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 5.0  # gave up -- did not hang waiting for a tier that never recovers
    assert result["result"]["pacing"]["cdp_backpressure_events"] == 1
    # Dispatch proceeded despite the still-degraded tier -- one real command
    # actually reached the (fake) device.
    assert len(client.cdp_dispatch_times) == 1


@pytest.mark.asyncio
async def test_cdp_backpressure_max_wait_s_zero_disables_tier_checking(tmp_path: Path) -> None:
    """`cdp_backpressure_max_wait_s=0` must skip tier checking entirely -- no
    `list_devices()` calls beyond the one `run_archive` itself always makes
    up front to resolve capabilities."""
    client = _basic_client(tabs=[_tab(101), _tab(102)], capabilities={"debugger": True})
    before = client.list_devices_calls
    await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0,
    )
    # Exactly the one up-front call in `run_archive` itself -- the pacer never
    # calls `list_devices()` when backpressure is disabled.
    assert client.list_devices_calls - before == 1


# ---------------------------------------------------------------------------
# Impossible depth -- fail loud, never silently degrade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l4_without_debugger_raises_before_writing_anything(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": False})
    with pytest.raises(ArchiveError) as exc_info:
        await run_archive(client, _DEVICE_ID, tmp_path, depth="L4")
    assert "debugger" in str(exc_info.value)
    assert "L4" in str(exc_info.value)
    # Nothing was written to disk -- a hard pre-flight failure creates no
    # archive directory at all.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_l5_without_debugger_also_raises(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": False})
    with pytest.raises(ArchiveError):
        await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")


@pytest.mark.asyncio
async def test_l3_without_debugger_does_not_raise_it_just_cannot_use_capture_hidden(tmp_path: Path) -> None:
    """L3 (screenshots) has no unconditional CDP requirement -- only L4/L5 do.
    A device without `debugger` can still run L3; screenshot simply omits
    capture_hidden and may fail per-tab (recorded, not a hard stop) for a
    background tab."""
    client = _basic_client(capabilities={"debugger": False}, tabs=[_tab(101)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L3")
    assert result["ok"] is True
    screenshot_calls = [c for c in client.calls if c[1] == "screenshot"]
    assert screenshot_calls and "capture_hidden" not in screenshot_calls[0][2]


@pytest.mark.asyncio
async def test_l3_with_debugger_uses_capture_hidden(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True}, tabs=[_tab(101)])
    await run_archive(client, _DEVICE_ID, tmp_path, depth="L3")
    screenshot_calls = [c for c in client.calls if c[1] == "screenshot"]
    assert screenshot_calls[0][2].get("capture_hidden") is True


@pytest.mark.asyncio
async def test_unknown_depth_raises_before_any_hub_call(tmp_path: Path) -> None:
    client = _basic_client()
    with pytest.raises(ArchiveError) as exc_info:
        await run_archive(client, _DEVICE_ID, tmp_path, depth="L99")
    assert "L99" in str(exc_info.value)
    assert client.calls == []  # not even list_devices()-adjacent work happened


@pytest.mark.asyncio
async def test_unknown_device_raises() -> None:
    client = FakeArchiveClient([], {})
    with pytest.raises(ArchiveError, match="unknown device"):
        await run_archive(client, "no-such-device", "/tmp/whatever", depth="L0")


# ---------------------------------------------------------------------------
# Cookies opt-in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cookies_never_collected_by_default_even_at_l5(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]
    assert manifest["profile"]["cookies"]["status"] == "skipped"
    assert not any(cmd == "cookies_list" for (_dev, cmd, _args) in client.calls)


@pytest.mark.asyncio
async def test_cookies_collected_only_with_explicit_opt_in(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5", include_cookies=True)
    manifest = result["result"]
    assert manifest["profile"]["cookies"]["status"] == "ok"
    assert any(cmd == "cookies_list" for (_dev, cmd, _args) in client.calls)


# ---------------------------------------------------------------------------
# Manifest shape / disk layout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_never_the_payload_only_paths_counts_bytes(tmp_path: Path) -> None:
    """The load-bearing constraint: run_archive's own return value must never
    contain a raw captured payload (page text, HTML, base64 image bytes,
    MHTML) -- only paths/counts/byte sizes/status."""
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]
    serialized = json.dumps(manifest)
    assert "hello world" not in serialized  # the read() text
    assert "<html><body>hi</body></html>" not in serialized  # the page_state outerHTML
    assert "aGVsbG8=" not in serialized  # the screenshot base64
    assert "MHTML-DATA" not in serialized  # the mhtml data


@pytest.mark.asyncio
async def test_archive_directory_and_manifest_json_are_actually_written(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L2")
    manifest = result["result"]
    archive_dir = Path(manifest["archive_dir"])
    assert archive_dir.is_dir()
    assert (archive_dir / "manifest.json").is_file()
    on_disk_manifest = json.loads((archive_dir / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk_manifest["device_id"] == _DEVICE_ID
    assert (archive_dir / "windows.json").is_file()
    assert (archive_dir / "tabs.json").is_file()
    for tab_id in manifest["tabs"]:
        tab_dir = archive_dir / "tabs" / tab_id
        assert (tab_dir / "text.txt").is_file()
        assert (tab_dir / "dom.html").is_file()
        assert (tab_dir / "page_state.json").is_file()


@pytest.mark.asyncio
async def test_tab_ids_filter_restricts_per_tab_capture_but_not_inventory(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101), _tab(102), _tab(103)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", tab_ids=[101])
    manifest = result["result"]
    assert manifest["tabs_inventory"]["count"] == 3  # inventory is always complete
    assert set(manifest["tabs"]) == {"101"}


@pytest.mark.asyncio
async def test_tab_ids_filter_reports_ids_that_were_not_found(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", tab_ids=[101, 999])
    manifest = result["result"]
    assert manifest["requested_tab_ids_not_found"] == [999]


# ---------------------------------------------------------------------------
# A requested-but-vanished tab_id must never be invisible: it is a FIFTH
# per-tab state ("not_found"), distinct from ok/partial/failed/skipped, and
# `summary` must account for every id the caller passed. Observed live: an
# archive requesting 4 tab_ids reported `tabs_capture_attempted: 3` with
# entries for only 3 of the 4 -- the 4th (closed between inventory and
# capture) appeared in no `tabs` entry, no `failures` entry, no `skipped`
# record, nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_found_tab_id_gets_its_own_manifest_tabs_entry(tmp_path: Path) -> None:
    """The load-bearing assertion: a tab_id absent from the live inventory is
    NOT simply missing from `manifest["tabs"]` -- it gets a real entry there,
    the same dict every other tab's outcome lives in, so a caller scanning
    `manifest["tabs"]` sees all four requested ids accounted for."""
    client = _basic_client(tabs=[_tab(101), _tab(102), _tab(103)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", tab_ids=[101, 102, 103, 999])
    manifest = result["result"]

    assert set(manifest["tabs"]) == {"101", "102", "103", "999"}
    assert manifest["tabs"]["999"]["status"] == "not_found"
    assert "reason" in manifest["tabs"]["999"]
    assert manifest["tabs"]["101"]["status"] == "ok"
    assert manifest["tabs"]["102"]["status"] == "ok"
    assert manifest["tabs"]["103"]["status"] == "ok"


@pytest.mark.asyncio
async def test_summary_accounts_for_every_requested_tab_id(tmp_path: Path) -> None:
    """`tabs_capture_attempted` (the tabs actually found and contacted) plus
    `tabs_not_found` (the ones that vanished) must equal the total number of
    ids the caller passed in `tab_ids` -- none may fall through the cracks."""
    client = _basic_client(tabs=[_tab(101), _tab(102), _tab(103)])
    requested = [101, 102, 103, 999]
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", tab_ids=requested)
    summary = result["result"]["summary"]

    assert summary["tabs_not_found"] == 1
    assert summary["tabs_capture_attempted"] == 3
    assert summary["tabs_capture_attempted"] + summary["tabs_not_found"] == len(requested)


@pytest.mark.asyncio
async def test_not_found_tab_is_benign_not_a_failure_but_not_plain_ok_either(
    tmp_path: Path,
) -> None:
    """A vanished tab is not a capture failure -- "closed before we got to it" is a
    legitimate, expected outcome, so it must never add an entry to
    `manifest["failures"]` nor push `manifest["status"]` to `"ok_with_failures"`.
    But it must also never be silently absorbed into a plain `"ok"` run -- the
    same non-negotiable already established for `"skipped"` tabs (no-wake
    guarantee), the other benign-but-not-clean outcome."""
    client = _basic_client(tabs=[_tab(101)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", tab_ids=[101, 999])
    manifest = result["result"]

    assert not any(f.get("tab_id") == 999 for f in manifest["failures"])
    assert manifest["status"] == "ok_with_skips"
    assert manifest["status"] != "ok"
    assert manifest["status"] != "ok_with_failures"
    assert manifest["summary"]["has_failures"] is False


@pytest.mark.asyncio
async def test_not_found_is_distinct_from_ok_partial_failed_and_skipped(tmp_path: Path) -> None:
    """All five per-tab states in one run: a clean capture, a discarded/skipped
    tab, a totally-failed tab, and a requested-but-vanished tab_id -- each must
    report its own distinct status and be counted in its own summary bucket,
    never conflated with any of the others."""
    client = _basic_client(
        tabs=[_tab(101), _tab(102, discarded=True), _tab(103)],
        extra_commands={
            "read": [
                {"ok": True, "result": {"url": "https://example.com", "title": "Test Page", "text": "hi"}},
                {"ok": False, "error": "boom"},
            ],
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", tab_ids=[101, 102, 103, 999])
    manifest = result["result"]

    assert manifest["tabs"]["101"]["status"] == "ok"
    assert manifest["tabs"]["102"]["status"] == "skipped"
    assert manifest["tabs"]["103"]["status"] == "failed"
    assert manifest["tabs"]["999"]["status"] == "not_found"

    summary = manifest["summary"]
    assert summary["tabs_captured"] == 1
    assert summary["tabs_skipped"] == 1
    assert summary["tabs_failed"] == 1
    assert summary["tabs_not_found"] == 1
    assert summary["tabs_capture_attempted"] == 3  # 101, 102, 103 -- not 999


@pytest.mark.asyncio
async def test_no_tab_ids_means_no_not_found_accounting(tmp_path: Path) -> None:
    """When the caller doesn't restrict to specific tab_ids, there is nothing to
    report as "not found" -- `tabs_not_found` stays 0 and no synthetic entries
    are added to `manifest["tabs"]`."""
    client = _basic_client(tabs=[_tab(101), _tab(102)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]

    assert manifest["summary"]["tabs_not_found"] == 0
    assert "requested_tab_ids_not_found" not in manifest
    assert set(manifest["tabs"]) == {"101", "102"}


# ---------------------------------------------------------------------------
# Live gap: L0 silently dropped a requested-but-nonexistent tab_id
#
# The not_found fix above lived inside the same `if depth_idx >= L1:` gate as
# the per-tab CAPTURE loop -- correct for capture (L0 does none, by design),
# wrong for this accounting (it doesn't need any capture to run at all: the
# full tab inventory is already read at every depth, including L0). Observed
# live against a real browser, requesting a tab_id that definitely does not
# exist (999999999):
#
#   L0:  status=ok              tabs_not_found=0   tabs=[]              <- silent
#   L1:  status=ok_with_skips   tabs_not_found=1   tabs=['999999999']   <- correct
#
# At L0 the caller explicitly asked for a tab that doesn't exist and got back
# a clean "ok" with no mention of it anywhere. None of the pre-existing
# not_found tests above would have caught this: every one of them requests at
# least L1, which is exactly the depth this bug did not reproduce at.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l0_reports_a_requested_but_nonexistent_tab_id_as_not_found(tmp_path: Path) -> None:
    """The load-bearing regression test for this fix: at L0 (no page contact at
    all), a `tab_ids` entry absent from the live inventory must still get a
    `manifest["tabs"][tab_id] = {"status": "not_found", ...}` entry and move
    `manifest["status"]` off plain "ok" -- exactly like it already does at L1+.
    Before the fix, `manifest["tabs"]` was `{}` and `status` was plain `"ok"`
    at L0, because the not_found loop lived inside the `depth_idx >= L1` gate
    that L0 never enters."""
    client = _basic_client(tabs=[_tab(101), _tab(102)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0", tab_ids=[101, 999999999])
    manifest = result["result"]

    assert manifest["tabs"]["999999999"]["status"] == "not_found"
    assert "reason" in manifest["tabs"]["999999999"]
    assert manifest["summary"]["tabs_not_found"] == 1
    assert manifest["status"] == "ok_with_skips"
    assert manifest["status"] != "ok"
    assert manifest["requested_tab_ids_not_found"] == [999999999]
    # L0 still does zero page contact -- this fix must not smuggle in a capture.
    per_tab_commands = {"read", "page_state", "screenshot", "mhtml", "nav_history"}
    assert not any(cmd in per_tab_commands for (_dev, cmd, _args) in client.calls)


@pytest.mark.asyncio
async def test_l0_reports_not_found_when_only_a_nonexistent_tab_id_is_requested(
    tmp_path: Path,
) -> None:
    """The exact live repro: the caller requests ONLY a tab_id that does not
    exist (no other real tabs named), at L0. `manifest["tabs"]` must not come
    back empty."""
    client = _basic_client(tabs=[_tab(101)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0", tab_ids=[999999999])
    manifest = result["result"]

    assert manifest["tabs"] != {}
    assert set(manifest["tabs"]) == {"999999999"}
    assert manifest["tabs"]["999999999"]["status"] == "not_found"
    assert manifest["status"] == "ok_with_skips"


@pytest.mark.parametrize("depth", ["L0", "L1", "L2", "L3", "L4", "L5"])
@pytest.mark.asyncio
async def test_not_found_accounting_holds_at_every_depth_including_l0(tmp_path: Path, depth: str) -> None:
    """The structural guarantee this fix establishes: not_found accounting for
    an explicitly requested `tab_ids` entry must hold at EVERY depth, not just
    the depths that happen to run a per-tab capture loop. Parametrizing across
    the whole depth ladder is what would have caught this bug the first time --
    the pre-existing not_found tests all happened to only exercise L1, which is
    precisely why the L0 gap shipped unnoticed."""
    client = _basic_client(tabs=[_tab(101)], capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth=depth, tab_ids=[101, 999999999])
    manifest = result["result"]

    assert manifest["tabs"]["999999999"]["status"] == "not_found"
    assert manifest["summary"]["tabs_not_found"] == 1
    assert manifest["status"] == "ok_with_skips"
    assert not any(f.get("tab_id") == 999999999 for f in manifest["failures"])


@pytest.mark.asyncio
async def test_relative_dest_dir_with_tilde_is_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IMPLEMENTATION_PHILOSOPHY.md: any path that may contain `~` must be
    expanded -- a caller passing `~/archives` must not silently produce a
    literal `~` directory relative to cwd."""
    monkeypatch.setenv("HOME", str(tmp_path))
    client = _basic_client()
    result = await run_archive(client, _DEVICE_ID, "~/archives", depth="L0")
    archive_dir = Path(result["result"]["archive_dir"])
    assert archive_dir.is_relative_to(tmp_path / "archives")
    assert "~" not in str(archive_dir)


# ---------------------------------------------------------------------------
# Device health: recovering from a real disconnect, not just a slow one.
#
# Real-world finding: a live 62-tab L4 archive (captures=["mhtml",
# "screenshot", "nav_history"], wake=true) measured tabs_captured=36,
# tabs_failed=24, tabs_partial=2, duration_s=742.9 -- the failures were a
# cascade of "device ... disconnected mid-command", ~20 back-to-back
# "Detached while handling command"/CDP -32603 "Internal error" failures, and
# one "could not reach hub ... timed out waiting for a response". The
# pre-existing `_CdpPacer` backpressure (`cdp_backpressure_max_wait_s`, 20s
# default) assumes the device is merely SLOW, not genuinely disconnected, so
# every one of the ~24 remaining dispatches paid that wait, gave up, and
# fired straight into the same dead connection again.
#
# These tests simulate that exact failure signature -- a device that goes
# non-live mid-run and later recovers -- rather than only ever exercising the
# happy path (a test where the device never drops cannot catch this
# regression; see the task's own instruction).
#
# CORRECTION (see archive.py's module docstring "Per-tab CDP isolation"
# section): a LATER 28-tab eval proved "Detached while handling command"/CDP
# `-32603` are PER-TAB session symptoms, not device-wide ones -- only
# "disconnected mid-command"/"could not reach hub" are genuine device-level
# events. The classifier is now split (`_is_device_disconnect_error`,
# `_is_per_tab_cdp_session_error`); tests below that simulate a genuine
# device-wide trip now script the disconnect-only signatures, not the
# per-tab ones -- scripting the per-tab pair no longer trips anything (see
# the dedicated per-tab-isolation tests further down).
# ---------------------------------------------------------------------------


def test_is_device_disconnect_error_matches_only_the_genuine_device_signatures() -> None:
    """Unit-level proof that the DEVICE-level classifier recognizes only the
    two genuine connection/websocket signatures, case-insensitively, and does
    NOT flag either an ordinary per-page content error OR a per-tab CDP
    session fault as device-wide trouble -- that distinction is the whole
    point of this fix (archive.py's "Per-tab CDP isolation" section)."""
    assert _is_device_disconnect_error("device 16909b75-... disconnected mid-command") is True
    assert (
        _is_device_disconnect_error(
            "could not reach hub at ws://100.124.126.19:8900/agent: timed out waiting for a response"
        )
        is True
    )
    # Per-tab CDP session faults must NOT be misclassified as device-wide trouble
    # -- this is the direct fix for the pre-correction over-broad classifier.
    assert _is_device_disconnect_error("Detached while handling command.") is False
    assert _is_device_disconnect_error('{"code":-32603,"message":"Internal error"}') is False
    # Ordinary page-content failures must never be misclassified as device trouble.
    assert _is_device_disconnect_error("Frame with ID 0 is showing error page") is False
    assert _is_device_disconnect_error("boom") is False
    assert _is_device_disconnect_error(None) is False
    assert _is_device_disconnect_error("") is False


def test_is_per_tab_cdp_session_error_matches_only_the_per_tab_signatures() -> None:
    """Unit-level proof that the PER-TAB classifier recognizes exactly the two
    per-tab session-fault signatures, case-insensitively, and does NOT flag a
    genuine device-level disconnect or an ordinary per-page content error."""
    assert _is_per_tab_cdp_session_error("Detached while handling command.") is True
    assert _is_per_tab_cdp_session_error("DETACHED WHILE HANDLING COMMAND.") is True
    assert _is_per_tab_cdp_session_error('{"code":-32603,"message":"Internal error"}') is True
    # Genuine device-level disconnects must NOT be misclassified as a per-tab fault.
    assert _is_per_tab_cdp_session_error("device 16909b75-... disconnected mid-command") is False
    assert (
        _is_per_tab_cdp_session_error(
            "could not reach hub at ws://100.124.126.19:8900/agent: timed out waiting for a response"
        )
        is False
    )
    # Ordinary page-content failures must never be misclassified as a CDP fault.
    assert _is_per_tab_cdp_session_error("Frame with ID 0 is showing error page") is False
    assert _is_per_tab_cdp_session_error("boom") is False
    assert _is_per_tab_cdp_session_error(None) is False
    assert _is_per_tab_cdp_session_error("") is False


@pytest.mark.asyncio
async def test_device_disconnect_cascade_recovers_and_captures_later_tabs(tmp_path: Path) -> None:
    """THE load-bearing regression test for a GENUINE device-wide disconnect: a
    fake device whose CDP (`mhtml`) commands start failing with genuine
    device-disconnect signatures (`disconnected mid-command` -- NOT the
    per-tab `-32603`/`Detached while handling command` pair, which no longer
    trips anything -- see the per-tab-isolation tests further down) after 2
    successful dispatches, tier reporting non-`live` for a stretch, then
    recovering -- the run must RECOVER (once tier confirms `live` again) and
    capture the LATER tabs, rather than cascading the failure through every
    remaining tab. A client that never drops cannot exercise this path at
    all."""
    tier_sequence = iter(["live", "intermittent", "intermittent", "live"])

    def _devices() -> list[dict[str, Any]]:
        record = _device_record(debugger=True)
        record["tier"] = next(tier_sequence, "live")
        return [record]

    tabs = [_tab(101), _tab(102), _tab(103), _tab(104)]
    client = _basic_client(
        tabs=tabs,
        capabilities={"debugger": True},
        devices=_devices,
        extra_commands={
            "mhtml": [
                {"ok": False, "error": f"device {_DEVICE_ID} disconnected mid-command"},
                {"ok": False, "error": f"device {_DEVICE_ID} disconnected mid-command"},
                {"ok": True, "result": {"tab_id": 103, "format": "mhtml", "bytes": 5, "data": "MHTML-DATA"}},
                {"ok": True, "result": {"tab_id": 104, "format": "mhtml", "bytes": 5, "data": "MHTML-DATA"}},
            ]
        },
    )
    events: list[dict[str, Any]] = []
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        device_health_trip_threshold=2,
        device_recovery_max_wait_s=5.0,
        on_progress=events.append,
    )
    manifest = result["result"]

    # The two tabs during the trip window keep their honest per-tab failure --
    # never silently retried or hidden.
    assert manifest["tabs"]["101"]["status"] == "failed"
    assert manifest["tabs"]["102"]["status"] == "failed"

    # The load-bearing assertion: the LATER tabs, captured once the device is
    # confirmed back on `live`, succeed -- the failure did not cascade through
    # the rest of the batch.
    assert manifest["tabs"]["103"]["status"] == "ok"
    assert manifest["tabs"]["104"]["status"] == "ok"

    pacing = manifest["pacing"]
    assert pacing["device_health_signals"] == 2
    assert pacing["device_health_trips"] == 1
    assert pacing["device_health_recoveries"] == 1
    assert pacing["cdp_slowdown_multiplier"] == 1.0  # fully recovered -- no lasting slowdown

    event_names = [e["event"] for e in events]
    assert "device_health_signal" in event_names
    assert "device_health_trip" in event_names
    assert "device_recovered" in event_names


@pytest.mark.asyncio
async def test_device_recovery_gives_up_after_budget_and_applies_slowdown(tmp_path: Path) -> None:
    """If the device never reports `live` again within `device_recovery_max_wait_s`,
    the pacer must not hang forever, and must not silently resume at the exact
    cadence that just overwhelmed the device -- it degrades gracefully
    (doubles its own effective `cdp_pace_s`, capped) instead. Dispatch still
    proceeds afterward -- this is pacing, not a correctness gate."""
    client = _basic_client(
        tabs=[_tab(101), _tab(102), _tab(103)],
        capabilities={"debugger": True},
        devices=lambda: [{**_device_record(debugger=True), "tier": "dormant"}],
        extra_commands={
            "mhtml": [
                {"ok": False, "error": f"device {_DEVICE_ID} disconnected mid-command"},
                {"ok": False, "error": f"device {_DEVICE_ID} disconnected mid-command"},
                {"ok": True, "result": {"tab_id": 103, "format": "mhtml", "bytes": 5, "data": "MHTML-DATA"}},
            ]
        },
    )
    started = time.monotonic()
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.01,
        cdp_backpressure_max_wait_s=0.0,
        device_health_trip_threshold=2,
        device_recovery_max_wait_s=0.2,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 10.0  # gave up -- did not hang waiting for a device that never recovers
    manifest = result["result"]

    pacing = manifest["pacing"]
    assert pacing["device_health_trips"] == 1
    assert pacing["device_health_recoveries"] == 0  # never confirmed live again
    assert pacing["cdp_slowdown_multiplier"] > 1.0  # degraded gracefully, not silently unchanged

    # Dispatch still proceeded for the tab after the trip -- this is advisory
    # pacing, never a correctness gate; the run reaches every tab regardless.
    assert manifest["tabs"]["103"]["status"] == "ok"


@pytest.mark.asyncio
async def test_a_single_isolated_device_health_signal_does_not_trip(tmp_path: Path) -> None:
    """One genuine device-disconnect-signature failure, surrounded by ordinary
    successes/failures, must be recorded as a device-health SIGNAL (diagnostic)
    but must NOT trip the escalated recovery wait -- only a genuine RUN of
    `device_health_trip_threshold` CONSECUTIVE signals does that. This is the
    "distinct from a single per-tab content failure" requirement."""
    client = _basic_client(
        tabs=[_tab(101), _tab(102), _tab(103)],
        capabilities={"debugger": True},
        extra_commands={
            "mhtml": [
                {"ok": False, "error": "some ordinary page rendering problem"},
                {"ok": False, "error": f"device {_DEVICE_ID} disconnected mid-command"},
                {"ok": False, "error": "another ordinary page rendering problem"},
            ]
        },
    )
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        device_health_trip_threshold=2,
    )
    pacing = result["result"]["pacing"]
    assert pacing["device_health_signals"] == 1  # only the one real signature counted
    assert pacing["device_health_trips"] == 0  # never reached 2 CONSECUTIVE signals
    assert pacing["cdp_slowdown_multiplier"] == 1.0


@pytest.mark.asyncio
async def test_device_health_escalation_can_be_disabled(tmp_path: Path) -> None:
    """`device_health_trip_threshold=0` must disable the escalation entirely --
    every CDP-paced capture is still attempted and recorded normally, exactly
    the "0 disables" convention `cdp_backpressure_max_wait_s`/`cdp_pace_s`
    already established."""
    client = _basic_client(
        tabs=[_tab(101), _tab(102), _tab(103)],
        capabilities={"debugger": True},
        extra_commands={
            "mhtml": [
                {"ok": False, "error": f"device {_DEVICE_ID} disconnected mid-command"},
                {"ok": False, "error": f"device {_DEVICE_ID} disconnected mid-command"},
                {"ok": False, "error": f"device {_DEVICE_ID} disconnected mid-command"},
            ]
        },
    )
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        device_health_trip_threshold=0,
    )
    pacing = result["result"]["pacing"]
    assert pacing["device_health_signals"] == 3  # still tracked as a diagnostic
    assert pacing["device_health_trips"] == 0  # but escalation never engaged
    # Every tab's own failure is still recorded normally -- disabling
    # escalation never means disabling per-tab failure recording.
    for tab_id in ("101", "102", "103"):
        assert result["result"]["tabs"][tab_id]["status"] == "failed"


# ---------------------------------------------------------------------------
# Per-tab CDP isolation -- see archive.py's module docstring ("Per-tab CDP
# isolation: a screenshot detach must not doom mhtml on the same tab") for the
# two 28-tab eval runs (identical WITH and WITHOUT chunking engaged) this
# responds to. Per-tab manifest analysis of both runs named the mechanism:
# `screenshot` throws CDP `-32603` on a tab with no paintable surface, that
# detaches the tab's OWN CDP session, and the SUBSEQUENT `mhtml` dispatch for
# the SAME tab then fails with `Detached while handling command` -- a per-tab
# cascade, not a device-wide one. These tests exercise the two-part fix:
# reordering (mhtml/nav_history dispatch before screenshot) and re-scoping the
# device-health classifier so an isolated per-tab fault never consumes the
# device-recovery budget.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screenshot_32603_does_not_cascade_into_mhtml_on_the_same_tab(tmp_path: Path) -> None:
    """THE load-bearing regression test for this fix: a tab whose `screenshot`
    capture raises CDP `-32603` must still get its `mhtml` capture -- the
    exact eval finding (9/9 tabs: screenshot `-32603` first, then `mhtml`
    `Detached while handling command` on the SAME tab). Before this fix,
    `_capture_tab` dispatched `screenshot` (L3) before `mhtml` (L4), so a
    `-32603` detach on `screenshot` left `mhtml`'s dispatch inheriting a
    broken CDP session and failing too -- both captures failed, and the tab
    was reported wholly `"failed"`. After the fix, `mhtml` dispatches FIRST
    (while the session is still fresh) and succeeds regardless of what
    happens to `screenshot` afterward -- the tab is honestly `"partial"`
    (screenshot failed, mhtml ok), never `"failed"`, and never silently
    retried. THIS TEST MUST FAIL AGAINST THE PRE-FIX DISPATCH ORDER."""
    client = _basic_client(
        tabs=[_tab(101)],
        capabilities={"debugger": True},
        extra_commands={
            "screenshot": {"ok": False, "error": '{"code":-32603,"message":"Internal error"}'},
            "mhtml": {
                "ok": True,
                "result": {"tab_id": 101, "format": "mhtml", "bytes": 5, "data": "MHTML-DATA"},
            },
        },
    )
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["screenshot", "mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
    )
    manifest = result["result"]

    # The load-bearing assertions: mhtml survived the screenshot's own
    # detach, and the tab is reported partial -- not both-failed.
    assert manifest["tabs"]["101"]["captures"]["mhtml"]["status"] == "ok"
    assert manifest["tabs"]["101"]["captures"]["screenshot"]["status"] == "failed"
    assert manifest["tabs"]["101"]["status"] == "partial"

    # The actual mhtml bytes made it to disk -- not just an in-memory "ok".
    mhtml_path = Path(manifest["tabs"]["101"]["captures"]["mhtml"]["path"])
    assert mhtml_path.exists()
    assert mhtml_path.read_text(encoding="utf-8") == "MHTML-DATA"

    # An honest, non-cascading capture failure still shows up in `failures` --
    # this fix is about isolation, never about hiding the real screenshot fault.
    screenshot_failures = [f for f in manifest["failures"] if f.get("capture") == "screenshot"]
    assert len(screenshot_failures) == 1
    assert "-32603" in screenshot_failures[0]["error"]


@pytest.mark.asyncio
async def test_mhtml_dispatches_before_screenshot_at_l4(tmp_path: Path) -> None:
    """Mechanism-level proof of the reorder itself: at L4 (`mhtml`+`screenshot`),
    `mhtml` must be the FIRST CDP dispatch this tab makes, `screenshot` the
    LAST -- the direct fix for the eval's observed ordering (screenshot fails
    first, dooming the mhtml dispatched right after it, pre-fix)."""
    client = _basic_client(tabs=[_tab(101)], capabilities={"debugger": True})
    await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
    )
    cdp_commands = [command for (_, command, _) in client.calls if command in ("mhtml", "screenshot")]
    assert cdp_commands == ["mhtml", "screenshot"]


@pytest.mark.asyncio
async def test_isolated_per_tab_32603_does_not_trip_device_health_or_wait(tmp_path: Path) -> None:
    """An isolated per-tab `-32603`/`Detached while handling command` fault --
    even repeated across MANY tabs -- must NEVER increment
    `device_health_trips`, NEVER consume the 120s `device_recovery_max_wait_s`
    budget, and must NEVER even count as a `device_health_signal` (that
    counter, like the trip counter, is reserved for genuine device-level
    disconnects post-fix). This is the direct fix for the eval finding: 5
    health-trips per 28-tab run were this misclassification, not real device
    trouble, and were responsible for most of the run's 295s duration."""
    tabs = [_tab(100 + i) for i in range(6)]
    client = _basic_client(
        tabs=tabs,
        capabilities={"debugger": True},
        extra_commands={
            "screenshot": {"ok": False, "error": '{"code":-32603,"message":"Internal error"}'},
        },
    )
    started = time.monotonic()
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["screenshot", "mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        device_health_trip_threshold=2,
        device_recovery_max_wait_s=5.0,
    )
    elapsed = time.monotonic() - started

    pacing = result["result"]["pacing"]
    # The decisive proof: pre-fix, these six -32603 failures would have
    # counted as device-health signals (old classifier matched "-32603"),
    # tripped the escalation at threshold=2, and consumed a
    # `device_recovery_max_wait_s`-bounded wait -- exactly the eval's "5
    # health-trips per run, ~295s of mostly-wasted waiting" finding. Post-fix,
    # none of that ever engages for a per-tab-only fault.
    assert pacing["device_health_signals"] == 0
    assert pacing["device_health_trips"] == 0
    assert pacing["device_health_recoveries"] == 0
    # This fake device's tier never leaves "live", so even a wrongly-tripped
    # recovery wait would resolve near-instantly here -- this is a sanity
    # bound, not the primary proof (the pacing counts above are).
    assert elapsed < 5.0

    # Every tab's mhtml still succeeded despite its own screenshot failing --
    # per-tab isolation held for all six tabs, not just one.
    for tab in tabs:
        tab_id = str(tab["tab_id"])
        assert result["result"]["tabs"][tab_id]["captures"]["mhtml"]["status"] == "ok"
        assert result["result"]["tabs"][tab_id]["status"] == "partial"


@pytest.mark.asyncio
async def test_genuine_device_disconnect_still_trips_recovery_after_rescoping(tmp_path: Path) -> None:
    """Regression guard: re-scoping the classifier to exclude per-tab
    `-32603`/`Detached` must NOT regress the genuine case the recovery
    machinery was built for. A run of GENUINE device-disconnect signatures
    (`disconnected mid-command`) must still trip `device_health_trips` and
    still recover once the device reports `live` again -- the real 62-tab
    incident's mechanism, unaffected by this fix."""
    tier_sequence = iter(["live", "intermittent", "intermittent", "live"])

    def _devices() -> list[dict[str, Any]]:
        record = _device_record(debugger=True)
        record["tier"] = next(tier_sequence, "live")
        return [record]

    tabs = [_tab(101), _tab(102), _tab(103)]
    client = _basic_client(
        tabs=tabs,
        capabilities={"debugger": True},
        devices=_devices,
        extra_commands={
            "mhtml": [
                {"ok": False, "error": f"device {_DEVICE_ID} disconnected mid-command"},
                {"ok": False, "error": f"device {_DEVICE_ID} disconnected mid-command"},
                {"ok": True, "result": {"tab_id": 103, "format": "mhtml", "bytes": 5, "data": "MHTML-DATA"}},
            ]
        },
    )
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        device_health_trip_threshold=2,
        device_recovery_max_wait_s=5.0,
    )
    pacing = result["result"]["pacing"]
    assert pacing["device_health_signals"] == 2
    assert pacing["device_health_trips"] == 1
    assert pacing["device_health_recoveries"] == 1
    assert result["result"]["tabs"]["103"]["status"] == "ok"


@pytest.mark.asyncio
async def test_per_tab_detach_triggers_an_explicit_reattach_before_the_next_cdp_capture(
    tmp_path: Path,
) -> None:
    """Mechanism-level proof of the reattach isolation: once `mhtml`'s own
    result matches a per-tab CDP session fault, `_capture_tab` must issue an
    explicit `attach` for THIS tab before the NEXT CDP-based capture
    (`nav_history`) -- rather than assuming a fresh attach happens on its
    own. The `attach` call must never be recorded as its own entry in the
    tab's `captures` dict (it is orchestrator bookkeeping, not a capture)."""
    client = _basic_client(
        tabs=[_tab(101)],
        capabilities={"debugger": True},
        extra_commands={
            "mhtml": {"ok": False, "error": "Detached while handling command."},
        },
    )
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L5",
        captures=["mhtml", "nav_history"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
    )
    commands = [command for (_, command, _) in client.calls]
    assert "attach" in commands
    # The reattach happens AFTER mhtml's own failed dispatch and BEFORE
    # nav_history's -- not before mhtml (nothing preceded it) and not
    # redundantly repeated.
    assert commands.index("mhtml") < commands.index("attach") < commands.index("nav_history")
    assert commands.count("attach") == 1

    # `attach` is orchestrator bookkeeping only -- never surfaced as its own
    # capture entry, and nav_history still gets its normal outcome recorded.
    assert "attach" not in result["result"]["tabs"]["101"]["captures"]
    assert result["result"]["tabs"]["101"]["captures"]["nav_history"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Proactive burst bound -- independent of any failure signal (priority 2:
# bound the concurrent/burst load on the device before it has any chance to
# tip CDP over in the first place).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdp_burst_size_forces_a_proactive_cooldown(tmp_path: Path) -> None:
    """After `cdp_burst_size` consecutive CDP dispatches, a cooldown pause must
    be inserted regardless of tier or device health -- proactively bounding
    burst load rather than only reacting once a failure has already
    occurred."""
    tabs = [_tab(100 + i) for i in range(5)]
    client = _basic_client(tabs=tabs, capabilities={"debugger": True})
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        cdp_burst_size=3,
        cdp_burst_cooldown_s=0.01,
    )
    assert result["result"]["pacing"]["cdp_burst_cooldowns"] == 1
    # Every tab still captured -- the cooldown is a pause, never a drop.
    assert result["result"]["summary"]["tabs_captured"] == 5


# ---------------------------------------------------------------------------
# No regression: a small, healthy run's behavior and manifest shape must be
# unaffected by any of the new device-health/burst knobs at their defaults.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_small_healthy_run_is_unaffected_by_device_health_defaults(tmp_path: Path) -> None:
    """A small archive with no failures, using every new knob's DEFAULT value,
    must behave exactly as before this fix: plain `"ok"` status, zero
    device-health signals/trips, zero burst cooldowns, and no lasting
    slowdown."""
    client = _basic_client(tabs=[_tab(101), _tab(102)], capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L4", captures=["mhtml"])
    manifest = result["result"]

    assert manifest["status"] == "ok"
    assert manifest["tabs"]["101"]["status"] == "ok"
    assert manifest["tabs"]["102"]["status"] == "ok"

    pacing = manifest["pacing"]
    assert pacing["device_health_trip_threshold"] == DEFAULT_DEVICE_HEALTH_TRIP_THRESHOLD
    assert pacing["device_recovery_max_wait_s"] == DEFAULT_DEVICE_RECOVERY_MAX_WAIT_S
    assert pacing["cdp_burst_size"] == DEFAULT_CDP_BURST_SIZE
    assert pacing["cdp_burst_cooldown_s"] == DEFAULT_CDP_BURST_COOLDOWN_S
    assert pacing["device_health_signals"] == 0
    assert pacing["device_health_trips"] == 0
    assert pacing["device_health_recoveries"] == 0
    assert pacing["cdp_burst_cooldowns"] == 0
    assert pacing["cdp_slowdown_multiplier"] == 1.0


# ---------------------------------------------------------------------------
# Chunking -- see module docstring's "Chunking: keeping the device under its
# drop threshold in the first place" section. Two measured eval data points
# (same device/tabs, CDP-only L4, current cdp_burst_size/cooldown defaults):
#
#   12 tabs:  rate 0.75,  0 health_trips,  48s   -- device not stressed
#   28 tabs:  rate 0.571, 5 health_trips, 295s   -- device overwhelmed
#
# The failure is LOAD-DEPENDENT: dispatching ~28 CDP captures back-to-back
# drives the device past its drop threshold. `cdp_chunk_size` proactively
# keeps every burst well below that threshold by splitting a large tab_ids
# list into sequential sub-batches with a real settle (cooldown + tier
# confirmation) between them -- never within one.
#
# No live browser is exercised in any of these tests -- only the same
# duck-typed `FakeArchiveClient` used throughout this file.
# ---------------------------------------------------------------------------


def test_chunk_tabs_splits_into_expected_number_of_chunks() -> None:
    """Unit-level proof of `_chunk_tabs`'s own splitting arithmetic, isolated
    from the async run_archive machinery: 28 tabs at chunk_size 8 must split
    into exactly 4 chunks (8, 8, 8, 4), matching the task's own 28-tab eval
    scenario."""
    tabs = [_tab(100 + i) for i in range(28)]
    chunks = _chunk_tabs(tabs, 8)
    assert len(chunks) == 4
    assert [len(c) for c in chunks] == [8, 8, 8, 4]
    # Every original tab appears in exactly one chunk, in order -- chunking
    # must never drop, duplicate, or reorder a tab.
    assert [t["tab_id"] for chunk in chunks for t in chunk] == [t["tab_id"] for t in tabs]


def test_chunk_tabs_disabled_by_zero_or_none_is_a_single_chunk() -> None:
    """`chunk_size<=0` or `None` must disable chunking entirely -- the whole
    list becomes ONE chunk, matching this module's established "0/None
    disables" convention."""
    tabs = [_tab(100 + i) for i in range(28)]
    assert _chunk_tabs(tabs, 0) == [tabs]
    assert _chunk_tabs(tabs, -1) == [tabs]
    assert _chunk_tabs(tabs, None) == [tabs]


def test_chunk_tabs_empty_list_is_zero_chunks() -> None:
    """An empty tab list produces zero chunks regardless of chunk_size, so
    callers can iterate the result unconditionally."""
    assert _chunk_tabs([], 8) == []
    assert _chunk_tabs([], None) == []


@pytest.mark.asyncio
async def test_large_run_splits_into_chunks_with_settle_between_not_within(tmp_path: Path) -> None:
    """THE load-bearing mechanism test: a 12-tab run (matching the task's own
    "did fine at 12" data point) with cdp_chunk_size=4 must split into exactly
    3 chunks, and a settle (cooldown + tier confirmation) must occur BETWEEN
    chunks (2 settles for 3 chunks) and NEVER within one -- proven by counting
    `list_devices()` calls attributable to the settle (a healthy device with
    `cdp_backpressure_max_wait_s=0` makes no OTHER tier-check calls once pacing
    itself is otherwise disabled)."""
    tabs = [_tab(100 + i) for i in range(12)]
    client = _basic_client(tabs=tabs, capabilities={"debugger": True})
    events: list[dict[str, Any]] = []
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        cdp_burst_size=0,
        cdp_chunk_size=4,
        cdp_chunk_cooldown_s=0.0,
        device_recovery_max_wait_s=1.0,
        on_progress=events.append,
    )
    manifest = result["result"]

    assert manifest["status"] == "ok"
    for i in range(12):
        assert manifest["tabs"][str(100 + i)]["status"] == "ok"

    pacing = manifest["pacing"]
    assert pacing["chunks_total"] == 3
    assert pacing["chunks_completed"] == 3
    assert pacing["chunk_settle_waits"] == 2  # between 3 chunks -- never after the last
    assert pacing["chunk_settle_gave_up"] is False

    # Every chunk boundary emitted a "chunk_completed" event followed by a
    # settle-recovered event -- never a "chunk_completed" for the last chunk
    # (there is nothing to settle before -- there is no next chunk).
    chunk_completed_events = [e for e in events if e["event"] == "chunk_completed"]
    settle_recovered_events = [e for e in events if e["event"] == "chunk_settle_recovered"]
    assert len(chunk_completed_events) == 2
    assert len(settle_recovered_events) == 2
    assert [e["chunk_index"] for e in chunk_completed_events] == [0, 1]

    # The settle's tier-confirmation reuses `_CdpPacer`'s own tier-check path
    # (`pacing["cdp_tier_checks"]`, incremented only by the pacer itself --
    # NOT by run_archive's own one-time initial device lookup). A healthy
    # device confirms live on the FIRST check every time (no backoff looping
    # needed), so exactly one tier check per settle: 2 settles -> 2 checks --
    # never within a chunk (backpressure/burst are both disabled here, so
    # these two are attributable to chunking alone).
    assert pacing["cdp_tier_checks"] == 2
    # The one-time device lookup at the top of run_archive is a plain
    # `list_devices()` call outside the pacer entirely -- so the raw call
    # count is the pacer's tier checks PLUS that one.
    assert client.list_devices_calls == pacing["cdp_tier_checks"] + 1


@pytest.mark.asyncio
async def test_non_live_tier_at_chunk_boundary_waits_then_proceeds_once_live(tmp_path: Path) -> None:
    """If the fake device reports a non-live tier right at a chunk boundary,
    the run must WAIT (bounded) rather than proceed immediately, and only
    dispatch the next chunk once tier reads live again -- the settle is a
    real gate, not a no-op. Only ONE list_devices() call happens outside the
    settle (run_archive's own one-time initial device lookup) -- everything
    after that is either per-tab dispatch (unpaced here: backpressure/burst
    are both disabled) or the settle's own tier-check loop, so the tier
    sequence below is consumed 1 (initial) + N (settle checks)."""
    tier_sequence = iter(["live", "intermittent", "intermittent", "live"])

    def _devices() -> list[dict[str, Any]]:
        record = _device_record(debugger=True)
        record["tier"] = next(tier_sequence, "live")
        return [record]

    tabs = [_tab(100 + i) for i in range(6)]  # 2 chunks of 3
    client = _basic_client(tabs=tabs, capabilities={"debugger": True}, devices=_devices)
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        cdp_burst_size=0,
        cdp_chunk_size=3,
        cdp_chunk_cooldown_s=0.0,
        device_recovery_max_wait_s=5.0,
    )
    manifest = result["result"]

    # Every tab in both chunks still captured successfully -- the settle
    # waited for recovery rather than giving up or proceeding blindly.
    for i in range(6):
        assert manifest["tabs"][str(100 + i)]["status"] == "ok"

    pacing = manifest["pacing"]
    assert pacing["chunks_total"] == 2
    assert pacing["chunks_completed"] == 2
    assert pacing["chunk_settle_gave_up"] is False
    # The settle had to poll more than once (tier was "intermittent" for a
    # stretch: the 2nd and 3rd scripted tier values) before confirming live
    # (the 4th) -- proof this was a real bounded wait, not an instant
    # pass-through. `cdp_tier_checks` is the pacer's OWN counter (excludes
    # run_archive's one-time initial device lookup, which is a plain
    # `list_devices()` call outside the pacer).
    assert pacing["cdp_tier_checks"] >= 2
    assert client.list_devices_calls == pacing["cdp_tier_checks"] + 1


@pytest.mark.asyncio
async def test_chunk_settle_gives_up_and_records_remaining_tabs_as_honest_failures(
    tmp_path: Path,
) -> None:
    """If the device NEVER recovers within device_recovery_max_wait_s, the run
    must not hang, must not silently drop the remaining tabs, and must not
    cascade one-by-one failures through them either: every tab in every
    not-yet-started chunk gets ONE honest 'failed' entry, and the run stops
    dispatching further chunks entirely."""
    tabs = [_tab(100 + i) for i in range(9)]  # 3 chunks of 3
    client = _basic_client(
        tabs=tabs,
        capabilities={"debugger": True},
        devices=lambda: [{**_device_record(debugger=True), "tier": "dormant"}],
    )
    started = time.monotonic()
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        cdp_burst_size=0,
        cdp_chunk_size=3,
        cdp_chunk_cooldown_s=0.01,
        device_recovery_max_wait_s=0.3,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 10.0  # bounded -- never hangs waiting for a device that never recovers
    manifest = result["result"]

    # First chunk (tabs 100-102) captured normally -- the device was live for it.
    for i in range(3):
        assert manifest["tabs"][str(100 + i)]["status"] == "ok"

    # Every tab in every LATER chunk (103-108) is an honest, distinct failure --
    # never silently dropped, never absent from the manifest.
    for i in range(3, 9):
        tab_id = str(100 + i)
        assert manifest["tabs"][tab_id]["status"] == "failed"
        assert "did not confirm" in manifest["tabs"][tab_id]["reason"]

    # Real per-tab failure entries, one per aborted tab -- never a silent drop.
    tab_failure_ids = {f["tab_id"] for f in manifest["failures"] if f["scope"] == "tab"}
    assert tab_failure_ids == {103, 104, 105, 106, 107, 108}

    assert manifest["status"] == "ok_with_failures"
    pacing = manifest["pacing"]
    assert pacing["chunks_total"] == 3
    assert pacing["chunks_completed"] == 1  # only the first chunk ever finished
    assert pacing["chunk_settle_gave_up"] is True
    assert pacing["chunk_settle_waits"] == 1  # stopped after the FIRST failed settle

    # Summary accounting must stay honest: 3 captured, 6 failed, 9 attempted total.
    assert manifest["summary"]["tabs_captured"] == 3
    assert manifest["summary"]["tabs_failed"] == 6
    assert manifest["summary"]["tabs_capture_attempted"] == 9


@pytest.mark.asyncio
async def test_chunking_composes_with_mid_chunk_device_recovery(tmp_path: Path) -> None:
    """Chunking must COMPOSE with the existing reactive mid-chunk recovery,
    not fight it: a device-health-signature drop INSIDE a chunk still
    recovers via the pre-existing `_CdpPacer` escalation, and the run still
    proceeds through the chunk boundary settle to the next chunk normally."""
    tier_sequence = iter(["live", "intermittent", "intermittent", "live", "live", "live"])

    def _devices() -> list[dict[str, Any]]:
        record = _device_record(debugger=True)
        record["tier"] = next(tier_sequence, "live")
        return [record]

    # 6 tabs, chunk_size=4 -> chunks = [[101,102,103,104], [105,106]]. The
    # device-health drop/recovery happens INSIDE chunk 1 (tabs 101/102 fail,
    # 103/104 succeed once recovered) -- proving chunking doesn't interfere
    # with the reactive escalation. Chunk 2 (105/106) then succeeds normally
    # after an ordinary (already-live) settle.
    tabs = [_tab(101), _tab(102), _tab(103), _tab(104), _tab(105), _tab(106)]
    client = _basic_client(
        tabs=tabs,
        capabilities={"debugger": True},
        devices=_devices,
        extra_commands={
            "mhtml": [
                {"ok": False, "error": f"device {_DEVICE_ID} disconnected mid-command"},
                {"ok": False, "error": f"device {_DEVICE_ID} disconnected mid-command"},
                {"ok": True, "result": {"tab_id": 103, "format": "mhtml", "bytes": 5, "data": "D"}},
                {"ok": True, "result": {"tab_id": 104, "format": "mhtml", "bytes": 5, "data": "D"}},
                {"ok": True, "result": {"tab_id": 105, "format": "mhtml", "bytes": 5, "data": "D"}},
                {"ok": True, "result": {"tab_id": 106, "format": "mhtml", "bytes": 5, "data": "D"}},
            ]
        },
    )
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        cdp_burst_size=0,
        device_health_trip_threshold=2,
        device_recovery_max_wait_s=5.0,
        cdp_chunk_size=4,
        cdp_chunk_cooldown_s=0.0,
    )
    manifest = result["result"]

    # Mid-chunk reactive recovery is unaffected: the two tabs during the trip
    # window keep their honest failure, the two after recovery succeed --
    # exactly the pre-existing device-health-recovery behavior.
    assert manifest["tabs"]["101"]["status"] == "failed"
    assert manifest["tabs"]["102"]["status"] == "failed"
    assert manifest["tabs"]["103"]["status"] == "ok"
    assert manifest["tabs"]["104"]["status"] == "ok"

    # The chunk boundary settle (between chunk 1 and chunk 2) then succeeds
    # normally (device already live by then), and chunk 2 captures fine.
    assert manifest["tabs"]["105"]["status"] == "ok"
    assert manifest["tabs"]["106"]["status"] == "ok"

    pacing = manifest["pacing"]
    assert pacing["chunks_total"] == 2
    assert pacing["chunks_completed"] == 2
    assert pacing["chunk_settle_gave_up"] is False
    assert pacing["device_health_trips"] == 1
    assert pacing["device_health_recoveries"] == 1


@pytest.mark.asyncio
async def test_cdp_chunk_size_zero_disables_chunking(tmp_path: Path) -> None:
    """`cdp_chunk_size=0` must disable chunking entirely: the whole tab_ids
    list is processed as a single continuous loop, exactly the pre-chunking
    behavior -- one chunk, zero settles, regardless of how large the run is."""
    tabs = [_tab(100 + i) for i in range(20)]
    client = _basic_client(tabs=tabs, capabilities={"debugger": True})
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        cdp_burst_size=0,
        cdp_chunk_size=0,
    )
    manifest = result["result"]
    assert manifest["summary"]["tabs_captured"] == 20
    pacing = manifest["pacing"]
    assert pacing["chunks_total"] == 1
    assert pacing["chunks_completed"] == 1
    assert pacing["chunk_settle_waits"] == 0
    assert pacing["chunk_settle_gave_up"] is False


@pytest.mark.asyncio
async def test_cdp_chunk_size_none_disables_chunking(tmp_path: Path) -> None:
    """`cdp_chunk_size=None` must behave identically to `0` -- both are the
    documented "disable chunking" spellings."""
    tabs = [_tab(100 + i) for i in range(20)]
    client = _basic_client(tabs=tabs, capabilities={"debugger": True})
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_pace_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        cdp_burst_size=0,
        cdp_chunk_size=None,
    )
    manifest = result["result"]
    assert manifest["summary"]["tabs_captured"] == 20
    pacing = manifest["pacing"]
    assert pacing["chunks_total"] == 1
    assert pacing["chunks_completed"] == 1
    assert pacing["chunk_settle_waits"] == 0


@pytest.mark.asyncio
async def test_small_run_at_or_under_chunk_size_is_a_single_chunk(tmp_path: Path) -> None:
    """A run with tabs <= cdp_chunk_size must be exactly ONE chunk with NO
    inter-chunk settle -- behaving exactly as it did before chunking existed,
    even with chunking enabled at its default size."""
    client = _basic_client(tabs=[_tab(101), _tab(102)], capabilities={"debugger": True})
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        cdp_chunk_size=8,
    )
    manifest = result["result"]
    assert manifest["status"] == "ok"
    assert manifest["tabs"]["101"]["status"] == "ok"
    assert manifest["tabs"]["102"]["status"] == "ok"

    pacing = manifest["pacing"]
    assert pacing["chunks_total"] == 1
    assert pacing["chunks_completed"] == 1
    assert pacing["chunk_settle_waits"] == 0
    assert pacing["chunk_settle_gave_up"] is False


@pytest.mark.asyncio
async def test_chunking_defaults_used_when_unspecified(tmp_path: Path) -> None:
    """A run that doesn't pass cdp_chunk_size/cdp_chunk_cooldown_s at all
    must report the module's real DEFAULT values in the manifest -- proving
    the defaults are actually wired into run_archive's signature, not just
    documented."""
    client = _basic_client(tabs=[_tab(101), _tab(102)], capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L4", captures=["mhtml"])
    pacing = result["result"]["pacing"]
    assert pacing["cdp_chunk_size"] == DEFAULT_CDP_CHUNK_SIZE
    assert pacing["cdp_chunk_cooldown_s"] == DEFAULT_CDP_CHUNK_COOLDOWN_S


@pytest.mark.asyncio
async def test_chunking_manifest_accounts_for_every_requested_tab_id(tmp_path: Path) -> None:
    """Manifest honesty must hold under chunking exactly as it does without
    it: every id in tab_ids gets an entry (ok or not_found), even when some
    ids are split across chunk boundaries and others don't exist at all."""
    tabs = [_tab(100 + i) for i in range(10)]
    client = _basic_client(tabs=tabs, capabilities={"debugger": True})
    requested = [100 + i for i in range(10)] + [999]  # 999 does not exist
    result = await run_archive(
        client,
        _DEVICE_ID,
        tmp_path,
        depth="L4",
        captures=["mhtml"],
        tab_ids=requested,
        cdp_chunk_size=4,
        cdp_chunk_cooldown_s=0.0,
        cdp_backpressure_max_wait_s=0.0,
        cdp_burst_size=0,
    )
    manifest = result["result"]
    for i in range(10):
        assert manifest["tabs"][str(100 + i)]["status"] == "ok"
    assert manifest["tabs"]["999"]["status"] == "not_found"
    assert manifest["requested_tab_ids_not_found"] == [999]

    pacing = manifest["pacing"]
    assert pacing["chunks_total"] == 3  # 10 tabs at chunk_size 4 -> 4,4,2
    assert pacing["chunks_completed"] == 3
    assert "chunk_settle_waits" in pacing
    assert "chunk_settle_wait_total_s" in pacing
