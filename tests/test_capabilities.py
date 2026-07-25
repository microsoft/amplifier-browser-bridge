"""Phase 4: the capability-probe re-probe/update path.

Phase 1 finding (design doc §2): `capture_visible_tab`/`scripting` can
under-report `false` at `hello` time if no real tab existed yet -- a capability
set that under-reports is worse than none, because an agent will route around
capabilities that actually exist. The fix is two-sided: the extension re-probes
once a real tab is available (background.js's `maybeReprobe`, not exercised
here -- JS is verified via `node --check` and manual reasoning, not this test
suite), and the hub accepts a `capabilities_update` message to correct its
record after `hello` -- that hub-side half is what these tests prove.

These tests exercise `Hub._handle_device_message` directly (the method
`_handle_device_ws`'s per-message-type dispatch was extracted into, precisely
so this is testable without a real WebSocket -- see hub.py's module
docstring). A minimal fake WebSocket satisfies the same narrow
`send_json`-only contract already established by registry.py's
`DeviceConnection` protocol.
"""

from __future__ import annotations

import asyncio
from typing import Any

from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import Hub


class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any], /) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        pass


def _hub(tmp_path: Any) -> Hub:
    return Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"))


def _hello(device_id: str = "d1", **caps: bool) -> dict[str, Any]:
    return {
        "v": 1,
        "id": "h1",
        "type": "hello",
        "device_id": device_id,
        "label": "edge-macos",
        "platform": "MacIntel",
        "capabilities": {
            "storage": True,
            "windows": True,
            "tab_groups": True,
            "debugger": True,
            "capture_visible_tab": False,
            "downloads": True,
            "alarms": True,
            "scripting": False,
            **caps,
        },
        "protocol_version": 1,
        "token": None,
    }


def test_hello_records_initial_capabilities(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    ws = _FakeWs()

    async def run() -> None:
        await hub._handle_device_message(ws, None, _hello())

    asyncio.run(run())
    record = hub.registry.get("d1")
    assert record is not None
    assert record.capabilities["capture_visible_tab"] is False
    assert record.capabilities["scripting"] is False


def test_capabilities_update_corrects_under_reported_capabilities(tmp_path: Any) -> None:
    """The core Phase 1 fix: an initial `hello` under-reports (no tab existed
    yet), and a later `capabilities_update` -- exactly what background.js's
    `maybeReprobe()` sends once a real tab appears -- corrects the record."""
    hub = _hub(tmp_path)
    ws = _FakeWs()

    async def run() -> None:
        await hub._handle_device_message(ws, None, _hello())
        await hub._handle_device_message(
            ws,
            "d1",
            {
                "v": 1,
                "id": "u1",
                "type": "capabilities_update",
                "device_id": "d1",
                "capabilities": {
                    "storage": True,
                    "windows": True,
                    "tab_groups": True,
                    "debugger": True,
                    "capture_visible_tab": True,
                    "downloads": True,
                    "alarms": True,
                    "scripting": True,
                },
            },
        )

    asyncio.run(run())
    record = hub.registry.get("d1")
    assert record is not None
    assert record.capabilities["capture_visible_tab"] is True
    assert record.capabilities["scripting"] is True
    # Untouched keys survive the merge.
    assert record.capabilities["debugger"] is True


def test_capabilities_update_merges_partial_keys(tmp_path: Any) -> None:
    """An update reporting only a subset of keys must not clobber the rest --
    merge semantics, not replace."""
    hub = _hub(tmp_path)
    ws = _FakeWs()

    async def run() -> None:
        await hub._handle_device_message(ws, None, _hello())
        await hub._handle_device_message(
            ws,
            "d1",
            {
                "v": 1,
                "id": "u1",
                "type": "capabilities_update",
                "device_id": "d1",
                "capabilities": {"scripting": True},
            },
        )

    asyncio.run(run())
    record = hub.registry.get("d1")
    assert record is not None
    assert record.capabilities["scripting"] is True
    assert record.capabilities["capture_visible_tab"] is False  # untouched
    assert record.capabilities["windows"] is True  # untouched


def test_capabilities_update_is_audited(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    ws = _FakeWs()

    async def run() -> None:
        await hub._handle_device_message(ws, None, _hello())
        await hub._handle_device_message(
            ws,
            "d1",
            {
                "v": 1,
                "id": "u1",
                "type": "capabilities_update",
                "device_id": "d1",
                "capabilities": {"scripting": True},
            },
        )

    asyncio.run(run())
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"event": "capabilities_updated"' in line for line in lines)


def test_capabilities_update_without_prior_hello_is_a_safe_noop(tmp_path: Any) -> None:
    """No device_id established yet (e.g. a malformed/out-of-order message) --
    must not raise, must not create a phantom device record."""
    hub = _hub(tmp_path)
    ws = _FakeWs()

    async def run() -> str | None:
        return await hub._handle_device_message(
            ws, None, {"v": 1, "id": "u1", "type": "capabilities_update", "capabilities": {"scripting": True}}
        )

    result = asyncio.run(run())
    assert result is None
    assert hub.registry.all() == []
