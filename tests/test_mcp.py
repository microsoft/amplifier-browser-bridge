"""Tests for the MCP server adapter (mcp_server.py).

The `@mcp.tool()` decorator returns the original function unchanged (see
`mcp.server.fastmcp.FastMCP.tool`'s `decorator`), so every `browser_*` function is
directly callable/awaitable here -- no MCP client/transport needed to exercise the
adapter logic itself. The end-to-end proof (a real MCP client driving a real
subprocess over stdio) is a separate, manual step -- see docs/AGENT_SURFACES.md.
"""

from __future__ import annotations

from typing import Any

import pytest

from amplifier_browser_bridge import HubError, Target
from amplifier_browser_bridge import mcp_server as srv


class _FakeHubClient:
    """Stands in for HubClient -- records every call, returns a canned response."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.command_calls: list[tuple[Target, str, dict[str, Any]]] = []
        self.list_devices_calls = 0
        self.poll_calls: list[tuple[str, str]] = []

    async def command(
        self, target: Target, command: str, args: dict[str, Any], *, session_id: str | None = None
    ) -> dict[str, Any]:
        self.command_calls.append((target, command, args))
        return self.response

    async def list_devices(self) -> list[dict[str, Any]]:
        self.list_devices_calls += 1
        return self.response.get("devices", [])

    async def poll(self, device_id: str, command_id: str) -> dict[str, Any]:
        self.poll_calls.append((device_id, command_id))
        return self.response


# ---------------------------------------------------------------------------
# Tool schema validity -- every tool FastMCP registered has a name, a
# non-empty description, and a JSON-schema-shaped input schema.
# ---------------------------------------------------------------------------


def test_all_expected_tools_are_registered():
    expected = {
        "browser_devices",
        "browser_tabs",
        "browser_snapshot",
        "browser_read",
        "browser_click",
        "browser_type",
        "browser_key",
        "browser_scroll",
        "browser_navigate",
        "browser_tab_open",
        "browser_tab_close",
        "browser_tab_activate",
        "browser_screenshot",
        "browser_vision_read",
        "browser_wait_for",
        "browser_wait_text",
        "browser_poll",
        "browser_confirm",
        "browser_establish_session",
        "browser_narrow_scope",
        "browser_fetch_bytes",
        "browser_grab_image",
        "browser_downloads_list",
        "browser_download",
        "browser_wait_download",
        "browser_archive",
        "browser_update_extension",
    }
    registered = {t.name for t in srv.mcp._tool_manager.list_tools()}
    assert registered == expected


def test_every_tool_has_a_nonempty_description_and_schema():
    for tool in srv.mcp._tool_manager.list_tools():
        assert tool.description and len(tool.description) > 10, tool.name
        schema = tool.parameters
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"


def test_browser_devices_and_browser_tabs_descriptions_teach_addressing():
    """The entry-point tools must teach an agent that has never seen this system
    that it needs to pick a device, then a tab, before acting."""
    tools = {t.name: t for t in srv.mcp._tool_manager.list_tools()}
    devices_desc = tools["browser_devices"].description or ""
    tabs_desc = tools["browser_tabs"].description or ""
    assert "first" in devices_desc.lower()
    assert "device_id" in tabs_desc or "device" in tabs_desc.lower()


def test_queue_note_present_on_every_tab_acting_tool_description():
    """Every tool that can target a non-live device must document the queued
    pass-through shape in its own description (an MCP client typically shows one
    tool's description in isolation)."""
    tab_acting = {
        "browser_tabs",
        "browser_snapshot",
        "browser_read",
        "browser_click",
        "browser_type",
        "browser_key",
        "browser_scroll",
        "browser_navigate",
        "browser_tab_open",
        "browser_tab_close",
        "browser_tab_activate",
        "browser_screenshot",
        "browser_wait_for",
        "browser_wait_text",
    }
    tools = {t.name: t for t in srv.mcp._tool_manager.list_tools()}
    for name in tab_acting:
        desc = tools[name].description or ""
        assert "queued" in desc, f"{name} description is missing the queued/tier note"
        assert "not an error" in desc.lower() or "not a hang" in desc.lower(), name


# ---------------------------------------------------------------------------
# Argument -> lib-call mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_click_maps_args_to_target_and_command(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeHubClient({"ok": True, "result": {"clicked": True}})
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_click(device_id="d1", tab_id=7, ref="e12")

    assert result == {"ok": True, "result": {"clicked": True}}
    assert len(fake.command_calls) == 1
    target, command, args = fake.command_calls[0]
    assert target == Target(device_id="d1", tab_id=7)
    assert command == "click"
    assert args == {"ref": "e12"}


@pytest.mark.asyncio
async def test_browser_type_maps_ref_and_text(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeHubClient({"ok": True, "result": {}})
    monkeypatch.setattr(srv, "_client", lambda: fake)

    await srv.browser_type(device_id="d1", tab_id=3, ref="e1", text="hello")

    _, command, args = fake.command_calls[0]
    assert command == "type"
    assert args == {"ref": "e1", "text": "hello"}


@pytest.mark.asyncio
async def test_browser_tab_open_defaults_to_background(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeHubClient({"ok": True, "result": {"tab_id": 99}})
    monkeypatch.setattr(srv, "_client", lambda: fake)

    await srv.browser_tab_open(device_id="d1")

    target, command, args = fake.command_calls[0]
    assert target == Target(device_id="d1")  # device-only -- no tab exists yet
    assert command == "tab_open"
    assert args == {"url": "about:blank", "active": False}


@pytest.mark.asyncio
async def test_browser_tabs_pages_and_reports_filters_and_totals(monkeypatch: pytest.MonkeyPatch):
    """browser_tabs must actually run its raw hub response through paging.py --
    not just fetch it (see paging.py's own unit tests for the shaping logic
    itself)."""
    tabs = [
        {"tab_id": i, "window_id": 1, "url": "https://example.com/", "title": "Example Page"}
        for i in range(5)
    ]
    fake = _FakeHubClient({"ok": True, "result": tabs})
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_tabs(device_id="d1", limit=2, offset=1)

    assert result["ok"] is True
    r = result["result"]
    assert r["total"] == 5
    assert r["matched"] == 5
    assert r["returned"] == 2
    assert r["offset"] == 1
    assert r["limit"] == 2
    assert r["has_more"] is True
    assert [t["tab_id"] for t in r["tabs"]] == [1, 2]


@pytest.mark.asyncio
async def test_browser_tabs_window_id_is_a_local_filter_not_forwarded_to_the_wire_target(
    monkeypatch: pytest.MonkeyPatch,
):
    """window_id narrows the RESPONSE (paging.py), not the wire-level Target --
    otherwise the reported `total` could never be the true, device-wide count."""
    tabs = [
        {"tab_id": 1, "window_id": 10, "url": "https://example.com/a", "title": "A"},
        {"tab_id": 2, "window_id": 20, "url": "https://example.com/b", "title": "B"},
    ]
    fake = _FakeHubClient({"ok": True, "result": tabs})
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_tabs(device_id="d1", window_id=10)

    target, command, _args = fake.command_calls[0]
    assert command == "tabs"
    assert target.window_id is None  # NOT forwarded to the hub/device
    r = result["result"]
    assert r["total"] == 2  # unfiltered grand total across the whole device
    assert r["matched"] == 1
    assert [t["tab_id"] for t in r["tabs"]] == [1]


@pytest.mark.asyncio
async def test_browser_tabs_summary_mode_returns_no_tab_list(monkeypatch: pytest.MonkeyPatch):
    tabs = [
        {"tab_id": 1, "window_id": 1, "url": "https://example.com/", "title": "Example", "discarded": True},
        {"tab_id": 2, "window_id": 2, "url": "https://example.org/", "title": "Other"},
    ]
    fake = _FakeHubClient({"ok": True, "result": tabs})
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_tabs(device_id="d1", summary=True)

    r = result["result"]
    assert "tabs" not in r
    assert r["summary"] is True
    assert r["total"] == 2


@pytest.mark.asyncio
async def test_browser_tabs_description_teaches_paging_and_summary():
    tools = {t.name: t for t in srv.mcp._tool_manager.list_tools()}
    desc = (tools["browser_tabs"].description or "").lower()
    assert "paged" in desc or "page" in desc
    assert "summary" in desc
    assert "has_more" in desc


@pytest.mark.asyncio
async def test_browser_devices_calls_list_devices_not_command(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeHubClient({"devices": [{"device_id": "d1", "tier": "live"}]})
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_devices()

    assert fake.list_devices_calls == 1
    assert fake.command_calls == []
    assert result == {"ok": True, "devices": [{"device_id": "d1", "tier": "live"}]}


@pytest.mark.asyncio
async def test_browser_poll_calls_poll_not_command(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeHubClient({"status": "pending"})
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_poll(device_id="d1", command_id="cmd-1")

    assert fake.poll_calls == [("d1", "cmd-1")]
    assert result == {"status": "pending"}


# ---------------------------------------------------------------------------
# The load-bearing guarantee: queued/tier results pass through untouched, and
# are never mistaken for an error or blocked on.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queued_result_passes_through_verbatim(monkeypatch: pytest.MonkeyPatch):
    queued_response = {
        "status": "queued",
        "command_id": "cmd-42",
        "tier": "intermittent",
        "last_seen": "2026-07-25T17:58:02.001+00:00",
        "queue_position": 1,
    }
    fake = _FakeHubClient(queued_response)
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_snapshot(device_id="d1", tab_id=7)

    # Bit-for-bit identical to what the hub returned -- not flattened into an
    # "ok" shape, not turned into an error, and (being a plain return, not an
    # await on some retry/backoff loop) not blocked on either.
    assert result == queued_response
    assert result["status"] == "queued"
    assert "ok" not in result
    assert "error" not in result


@pytest.mark.asyncio
async def test_dormant_device_queue_result_also_passes_through(monkeypatch: pytest.MonkeyPatch):
    dormant_response = {
        "status": "queued",
        "command_id": "cmd-7",
        "tier": "dormant",
        "last_seen": None,
        "queue_position": 1,
    }
    fake = _FakeHubClient(dormant_response)
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_tabs(device_id="phone-1")

    assert result == dormant_response
    assert result["tier"] == "dormant"


@pytest.mark.asyncio
async def test_hub_error_surfaces_as_ok_false_not_an_exception(monkeypatch: pytest.MonkeyPatch):
    class _RaisingClient:
        async def command(self, *a, **k):
            raise HubError("unauthorized")

    monkeypatch.setattr(srv, "_client", lambda: _RaisingClient())

    result = await srv.browser_read(device_id="d1", tab_id=1)

    assert result == {"ok": False, "error": "unauthorized"}


# ---------------------------------------------------------------------------
# browser_screenshot -- returns MCP image content blocks (pixels, no model call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_screenshot_returns_image_content_block(monkeypatch: pytest.MonkeyPatch):
    import base64

    from mcp.server.fastmcp import Image

    raw = b"\xff\xd8\xfake-jpeg"
    b64 = base64.b64encode(raw).decode("ascii")
    fake = _FakeHubClient(
        {"ok": True, "result": {"tab_id": 7, "format": "jpeg", "base64": b64, "via": "cdp"}}
    )
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_screenshot(device_id="d1", tab_id=7, capture_hidden=True)

    assert isinstance(result, list)
    assert isinstance(result[0], Image)
    assert result[0].data == raw
    assert isinstance(result[1], dict)
    assert "base64" not in result[1]  # replaced by the Image content block, not duplicated as text
    assert result[1]["tab_id"] == 7
    _, command, args = fake.command_calls[0]
    assert command == "screenshot"
    assert args == {"capture_hidden": True}


@pytest.mark.asyncio
async def test_browser_screenshot_multi_page_returns_one_image_per_page(monkeypatch: pytest.MonkeyPatch):
    import base64

    from mcp.server.fastmcp import Image

    pages = [
        {"index": i, "format": "jpeg", "base64": base64.b64encode(f"p{i}".encode()).decode()}
        for i in range(3)
    ]
    fake = _FakeHubClient(
        {"ok": True, "result": {"tab_id": 7, "pages": pages, "page_count": 3, "capped": False, "via": "cdp"}}
    )
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_screenshot(device_id="d1", tab_id=7, multi_page=True, max_pages=5)

    assert sum(1 for item in result if isinstance(item, Image)) == 3
    meta = next(item for item in result if isinstance(item, dict))
    assert meta["page_count"] == 3
    _, _, args = fake.command_calls[0]
    assert args == {"multi_page": True, "max_pages": 5}


@pytest.mark.asyncio
async def test_browser_screenshot_queued_result_passes_through_as_is(monkeypatch: pytest.MonkeyPatch):
    queued = {"status": "queued", "command_id": "cmd-1", "tier": "intermittent", "queue_position": 1}
    fake = _FakeHubClient(queued)
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_screenshot(device_id="d1", tab_id=7)

    assert result == queued  # no image to render -- pass the queued shape through untouched


# ---------------------------------------------------------------------------
# browser_vision_read -- distinct mechanism, real model call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_vision_read_composes_screenshot_and_extraction(monkeypatch: pytest.MonkeyPatch):
    import base64

    raw = b"\xff\xd8\xfake-jpeg"
    b64 = base64.b64encode(raw).decode("ascii")
    fake = _FakeHubClient(
        {"ok": True, "result": {"tab_id": 7, "format": "jpeg", "base64": b64, "via": "cdp"}}
    )
    monkeypatch.setattr(srv, "_client", lambda: fake)

    async def _fake_extract_text(images, prompt, **kwargs):
        return {
            "text": "extracted!",
            "provider": "anthropic",
            "model": "claude-3-5-sonnet-latest",
            "image_count": 1,
        }

    monkeypatch.setattr("amplifier_browser_bridge.vision_read.extract_text", _fake_extract_text)

    result = await srv.browser_vision_read(device_id="d1", tab_id=7, prompt="read this")

    assert result["ok"] is True
    assert result["result"]["text"] == "extracted!"
    assert result["result"]["vision_provider"] == "anthropic"
    # capture_hidden defaults to True for vision_read
    _, command, args = fake.command_calls[0]
    assert command == "screenshot"
    assert args["capture_hidden"] is True


@pytest.mark.asyncio
async def test_browser_vision_read_surfaces_config_error_as_ok_false(monkeypatch: pytest.MonkeyPatch):
    from amplifier_browser_bridge.vision import VisionConfigError

    raw = b"\xff\xd8\xfake-jpeg"
    import base64

    b64 = base64.b64encode(raw).decode("ascii")
    fake = _FakeHubClient({"ok": True, "result": {"tab_id": 7, "format": "jpeg", "base64": b64}})
    monkeypatch.setattr(srv, "_client", lambda: fake)

    async def _raise_config_error(*args, **kwargs):
        raise VisionConfigError("No vision provider is configured -- set ANTHROPIC_API_KEY, ...")

    monkeypatch.setattr("amplifier_browser_bridge.vision_read.extract_text", _raise_config_error)

    result = await srv.browser_vision_read(device_id="d1", tab_id=7)

    assert result["ok"] is False
    assert "No vision provider is configured" in result["error"]


# ---------------------------------------------------------------------------
# browser_archive -- D2, browser-state archive: proves this surface actually
# routes through archive.py's run_archive (not just fetches). See
# tests/test_archive.py for the orchestrator's own logic (depth ladder,
# no-wake guarantee, failure recording, impossible-depth) -- this file only
# proves the MCP adapter wiring.
# ---------------------------------------------------------------------------


class _ScriptedHubClient:
    """Same shape as _FakeHubClient but with per-command scripted responses --
    a plain constant response (_FakeHubClient) can't stand in for run_archive,
    which needs `windows` and `tabs` to return different shapes."""

    def __init__(self, devices: list[dict[str, Any]], by_command: dict[str, Any]) -> None:
        self._devices = devices
        self._by_command = by_command
        self.command_calls: list[tuple[Target, str, dict[str, Any]]] = []

    async def list_devices(self) -> list[dict[str, Any]]:
        return self._devices

    async def command(self, target: Target, command: str, args: dict[str, Any]) -> dict[str, Any]:
        self.command_calls.append((target, command, args))
        return self._by_command.get(command, {"ok": True, "result": {}})


def _archive_device(**capabilities: bool) -> dict[str, Any]:
    return {
        "device_id": "d1",
        "capabilities": {"debugger": False, "scripting": True, **capabilities},
    }


@pytest.mark.asyncio
async def test_browser_archive_routes_through_run_archive(tmp_path, monkeypatch: pytest.MonkeyPatch):
    fake = _ScriptedHubClient(
        [_archive_device()],
        {
            "windows": {"ok": True, "result": {"windows": [], "tab_groups": []}},
            "tabs": {"ok": True, "result": []},
        },
    )
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_archive(device_id="d1", dest_dir=str(tmp_path), depth="L0")

    assert result["ok"] is True
    manifest = result["result"]
    assert manifest["device_id"] == "d1"
    assert manifest["depth"] == "L0"
    assert (tmp_path.__class__(manifest["archive_dir"]) / "manifest.json").is_file()
    # Actually went through run_archive's own command sequence, not a stub.
    called_commands = {c for (_t, c, _a) in fake.command_calls}
    assert called_commands == {"windows", "tabs"}


@pytest.mark.asyncio
async def test_browser_archive_impossible_depth_surfaces_as_ok_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    fake = _ScriptedHubClient([_archive_device(debugger=False)], {})
    monkeypatch.setattr(srv, "_client", lambda: fake)

    result = await srv.browser_archive(device_id="d1", dest_dir=str(tmp_path), depth="L4")

    assert result["ok"] is False
    assert "debugger" in result["error"]


@pytest.mark.asyncio
async def test_browser_archive_hub_error_surfaces_as_ok_false(monkeypatch: pytest.MonkeyPatch, tmp_path):
    class _RaisingArchiveClient:
        async def list_devices(self):
            raise HubError("unauthorized")

        async def command(self, *a, **k):
            raise HubError("unauthorized")

    monkeypatch.setattr(srv, "_client", lambda: _RaisingArchiveClient())

    result = await srv.browser_archive(device_id="d1", dest_dir=str(tmp_path))

    assert result == {"ok": False, "error": "unauthorized"}
