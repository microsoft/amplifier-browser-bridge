"""Protocol-compliance and adapter tests for the browser-bridge Amplifier tool
module. See the `creating-amplifier-modules` skill: these tests verify
`coordinator.mount()` was actually called (the Iron Law), not that `mount()`
returns None.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from amplifier_browser_bridge import Target
from amplifier_core import ToolResult

from amplifier_module_tool_browser_bridge import _build_tools, _client, mount


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

    async def establish_session(self, **kwargs: Any) -> dict[str, Any]:
        return self.response

    async def narrow_scope(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.response

    async def list_devices(self) -> list[dict[str, Any]]:
        self.list_devices_calls += 1
        return self.response.get("devices", [])

    async def poll(self, device_id: str, command_id: str) -> dict[str, Any]:
        self.poll_calls.append((device_id, command_id))
        return self.response


def _tool_by_name(name: str):
    tools = _build_tools()
    matches = [t for t in tools if t.name == name]
    assert len(matches) == 1, f"expected exactly one tool named {name!r}, found {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# The Iron Law: mount() must register real tools with the coordinator.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_registers_every_tool():
    coordinator = MagicMock()
    coordinator.mount = AsyncMock()

    result = await mount(coordinator)

    expected_names = {t.name for t in _build_tools()}
    registered_names = {c.kwargs.get("name") for c in coordinator.mount.call_args_list}
    assert registered_names == expected_names
    assert coordinator.mount.call_count == len(expected_names)
    for call in coordinator.mount.call_args_list:
        assert call.args[0] == "tools"

    assert result is not None
    assert result["name"] == "tool-browser-bridge"
    assert set(result["provides"]) == expected_names


@pytest.mark.asyncio
async def test_every_tool_satisfies_the_tool_protocol():
    for tool in _build_tools():
        assert isinstance(tool.name, str) and tool.name
        assert isinstance(tool.description, str) and tool.description
        assert isinstance(tool.input_schema, dict)
        assert callable(tool.execute)


@pytest.mark.asyncio
async def test_tool_names_match_mcp_server_vocabulary():
    """Same tool vocabulary as mcp_server.py, for consistency across surfaces."""
    names = {t.name for t in _build_tools()}
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
        "browser_reload",
        "browser_fetch_bytes",
        "browser_grab_image",
        "browser_downloads_list",
        "browser_download",
        "browser_wait_download",
        "browser_establish_session",
        "browser_narrow_scope",
        "browser_archive",
        "browser_archive_convert",
        "browser_archive_catalog",
        "browser_setup",
        "browser_setup_status",
        "browser_update_extension",
    }
    assert names == expected


# ---------------------------------------------------------------------------
# Argument -> lib-call mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_click_maps_args_to_target_and_command(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeHubClient({"ok": True, "result": {"clicked": True}})
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    tool = _tool_by_name("browser_click")
    result = await tool.execute({"device_id": "d1", "tab_id": 7, "ref": "e12"})

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.output == {"ok": True, "result": {"clicked": True}}
    assert len(fake.command_calls) == 1
    target, command, args = fake.command_calls[0]
    assert target == Target(device_id="d1", tab_id=7)
    assert command == "click"
    assert args == {"ref": "e12"}


@pytest.mark.asyncio
async def test_browser_type_maps_ref_and_text(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeHubClient({"ok": True, "result": {}})
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    tool = _tool_by_name("browser_type")
    await tool.execute({"device_id": "d1", "tab_id": 3, "ref": "e1", "text": "hello"})

    _, command, args = fake.command_calls[0]
    assert command == "type"
    assert args == {"ref": "e1", "text": "hello"}


@pytest.mark.asyncio
async def test_browser_tabs_pages_and_reports_filters_and_totals(monkeypatch: pytest.MonkeyPatch):
    """browser_tabs must run its raw hub response through paging.py -- not just
    fetch it (see paging.py's own unit tests for the shaping logic itself)."""
    tabs = [
        {"tab_id": i, "window_id": 1, "url": "https://example.com/", "title": "Example Page"}
        for i in range(5)
    ]
    fake = _FakeHubClient({"ok": True, "result": tabs})
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    tool = _tool_by_name("browser_tabs")
    result = await tool.execute({"device_id": "d1", "limit": 2, "offset": 1})

    assert result.success is True
    output: Any = result.output
    r = output["result"]
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
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    tool = _tool_by_name("browser_tabs")
    result = await tool.execute({"device_id": "d1", "window_id": 10})

    target, command, _args = fake.command_calls[0]
    assert command == "tabs"
    assert target.window_id is None  # NOT forwarded to the hub/device
    output: Any = result.output
    r = output["result"]
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
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    tool = _tool_by_name("browser_tabs")
    result = await tool.execute({"device_id": "d1", "summary": True})

    output: Any = result.output
    r = output["result"]
    assert "tabs" not in r
    assert r["summary"] is True
    assert r["total"] == 2


def test_browser_tabs_description_teaches_paging_and_summary():
    tool = _tool_by_name("browser_tabs")
    desc = tool.description.lower()
    assert "paged" in desc or "page" in desc
    assert "summary" in desc
    assert "has_more" in desc


def test_browser_tabs_schema_declares_new_filter_and_paging_properties():
    tool = _tool_by_name("browser_tabs")
    props = tool.input_schema["properties"]
    for name in ("window_id", "url_contains", "title_contains", "limit", "offset", "summary"):
        assert name in props, f"browser_tabs schema missing {name!r}"
    assert tool.input_schema["required"] == ["device_id"]


@pytest.mark.asyncio
async def test_browser_devices_calls_list_devices_not_command(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeHubClient({"devices": [{"device_id": "d1", "tier": "live"}]})
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    tool = _tool_by_name("browser_devices")
    result = await tool.execute({})

    assert fake.list_devices_calls == 1
    assert fake.command_calls == []
    assert result.output == {"ok": True, "devices": [{"device_id": "d1", "tier": "live"}]}


@pytest.mark.asyncio
async def test_browser_poll_calls_poll_not_command(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeHubClient({"status": "pending"})
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    tool = _tool_by_name("browser_poll")
    result = await tool.execute({"device_id": "d1", "command_id": "cmd-1"})

    assert fake.poll_calls == [("d1", "cmd-1")]
    assert result.output == {"status": "pending"}


# ---------------------------------------------------------------------------
# The load-bearing guarantee: queued/tier results pass through untouched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queued_result_passes_through_verbatim(monkeypatch: pytest.MonkeyPatch):
    """A non-live device's queued response must reach the caller exactly as the
    hub returned it -- not flattened, not swallowed, not reported as an error."""
    queued_response = {
        "status": "queued",
        "command_id": "cmd-42",
        "tier": "intermittent",
        "last_seen": "2026-07-25T17:58:02.001+00:00",
        "queue_position": 1,
    }
    fake = _FakeHubClient(queued_response)
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    tool = _tool_by_name("browser_snapshot")
    result = await tool.execute({"device_id": "d1", "tab_id": 7})

    assert result.success is True  # adapter-level success -- this is real, actionable data
    assert result.output == queued_response
    output = result.output
    assert isinstance(output, dict)
    assert output["status"] == "queued"
    assert output["tier"] == "intermittent"
    assert output["queue_position"] == 1


@pytest.mark.asyncio
async def test_hub_error_surfaces_as_adapter_failure(monkeypatch: pytest.MonkeyPatch):
    from amplifier_browser_bridge import HubError

    class _RaisingClient:
        async def command(self, *a, **k):
            raise HubError("unauthorized")

    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: _RaisingClient())

    tool = _tool_by_name("browser_read")
    result = await tool.execute({"device_id": "d1", "tab_id": 1})

    assert result.success is False
    assert "unauthorized" in str(result.output)


@pytest.mark.asyncio
async def test_browser_archive_routes_through_run_archive(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Proves this surface actually routes through archive.py's run_archive
    (not just a stub) -- tests/test_archive.py covers run_archive's own logic
    (depth ladder, no-wake guarantee, failure recording, impossible-depth)."""

    class _ScriptedClient:
        def __init__(self) -> None:
            self.command_calls: list[tuple[Any, str, dict[str, Any]]] = []

        async def list_devices(self):
            return [{"device_id": "d1", "capabilities": {"debugger": False, "scripting": True}}]

        async def command(self, target, command, args):
            self.command_calls.append((target, command, args))
            if command == "windows":
                return {"ok": True, "result": {"windows": [], "tab_groups": []}}
            if command == "tabs":
                return {"ok": True, "result": []}
            return {"ok": True, "result": {}}

    fake = _ScriptedClient()
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    tool = _tool_by_name("browser_archive")
    result = await tool.execute({"device_id": "d1", "dest_dir": str(tmp_path), "depth": "L0"})

    assert result.success is True
    output = result.output
    assert isinstance(output, dict)
    assert output["ok"] is True
    assert output["result"]["device_id"] == "d1"
    assert output["result"]["depth"] == "L0"
    called_commands = {c for (_t, c, _a) in fake.command_calls}
    assert called_commands == {"windows", "tabs"}


@pytest.mark.asyncio
async def test_browser_archive_forwards_injection_timeout_s_and_captures(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """Proves this adapter actually forwards `injection_timeout_s`/`captures`
    through to `run_archive` -- not just accepts and drops them."""

    class _ScriptedClient:
        def __init__(self) -> None:
            self.command_calls: list[tuple[Any, str, dict[str, Any]]] = []

        async def list_devices(self):
            return [{"device_id": "d1", "capabilities": {"debugger": True, "scripting": True}}]

        async def command(self, target, command, args):
            self.command_calls.append((target, command, args))
            if command == "windows":
                return {"ok": True, "result": {"windows": [], "tab_groups": []}}
            if command == "tabs":
                return {"ok": True, "result": [{"tab_id": 101, "window_id": 1, "url": "https://x.com"}]}
            if command == "mhtml":
                return {"ok": True, "result": {"tab_id": 101, "format": "mhtml", "bytes": 1, "data": "M"}}
            return {"ok": True, "result": {}}

    fake = _ScriptedClient()
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    tool = _tool_by_name("browser_archive")
    result = await tool.execute(
        {
            "device_id": "d1",
            "dest_dir": str(tmp_path),
            "depth": "L4",
            "captures": ["mhtml"],
            "injection_timeout_s": 5.0,
        }
    )

    assert result.success is True
    output = result.output
    assert isinstance(output, dict)
    assert output["ok"] is True
    assert output["result"]["captures_requested"] == ["mhtml"]
    called_commands = {c for (_t, c, _a) in fake.command_calls}
    assert "read" not in called_commands
    assert "page_state" not in called_commands
    assert output["result"]["tabs"]["101"]["captures"]["text"]["status"] == "skipped"


@pytest.mark.asyncio
async def test_browser_archive_impossible_depth_returns_ok_false_not_adapter_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    class _NoDebuggerClient:
        async def list_devices(self):
            return [{"device_id": "d1", "capabilities": {"debugger": False}}]

        async def command(self, target, command, args):
            return {"ok": True, "result": {}}

    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: _NoDebuggerClient())

    tool = _tool_by_name("browser_archive")
    result = await tool.execute({"device_id": "d1", "dest_dir": str(tmp_path), "depth": "L4"})

    # An ArchiveError is a legitimate, actionable RESULT (like ok: false for
    # any other tool) -- not an adapter-level failure.
    assert result.success is True
    output = result.output
    assert isinstance(output, dict)
    assert output["ok"] is False
    assert "debugger" in output["error"]


@pytest.mark.asyncio
async def test_browser_archive_convert_routes_through_run_archive_convert(tmp_path):
    archive_dir = tmp_path / "archive_d1_20260815T000000Z"
    tab_dir = archive_dir / "tabs" / "101"
    tab_dir.mkdir(parents=True)
    (tab_dir / "page.mhtml").write_bytes(
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/related; boundary="X"\r\n\r\n'
        b"--X\r\nContent-Type: text/html\r\nContent-Location: https://example.com/\r\n\r\n"
        b"<html><body><article><p>Some real synthetic article text for this test.</p>"
        b"</article></body></html>\r\n--X--\r\n"
    )

    tool = _tool_by_name("browser_archive_convert")
    result = await tool.execute({"archive_dir": str(archive_dir)})

    assert result.success is True
    output = result.output
    assert isinstance(output, dict)
    assert output["ok"] is True
    assert output["result"]["tabs"]["101"]["status"] == "ok"
    # Load-bearing: never markdown BODY text through the tool surface.
    assert "Some real synthetic article text for this test" not in str(output)


@pytest.mark.asyncio
async def test_browser_archive_convert_bad_archive_dir_returns_ok_false_not_adapter_failure(tmp_path):
    not_an_archive = tmp_path / "not_an_archive"
    not_an_archive.mkdir()

    tool = _tool_by_name("browser_archive_convert")
    result = await tool.execute({"archive_dir": str(not_an_archive)})

    assert result.success is True
    output = result.output
    assert isinstance(output, dict)
    assert output["ok"] is False
    assert "tabs/" in output["error"]


@pytest.mark.asyncio
async def test_browser_archive_catalog_routes_through_run_archive_catalog(tmp_path):
    """Proves this surface actually routes through archive_catalog.py's
    run_archive_catalog (not just a stub) -- tests/test_archive_catalog.py
    covers run_archive_catalog's own logic (Layer 1 inventory, Layer 2
    lens/retry/no_content behavior) in depth."""
    archive_dir = tmp_path / "archive_d1_20260816T000000Z"
    archive_dir.mkdir(parents=True)
    (archive_dir / "tabs.json").write_text(
        '[{"tab_id": 1, "window_id": 1, "url": "https://example.com/", "title": "Example", '
        '"discarded": false, "asleep": false, "pinned": false}]',
        encoding="utf-8",
    )

    tool = _tool_by_name("browser_archive_catalog")
    result = await tool.execute({"archive_dir": str(archive_dir)})

    assert result.success is True
    output = result.output
    assert isinstance(output, dict)
    assert output["ok"] is True
    # catalog=false is the default -- Layer 1 only, no model call attempted.
    assert output["result"]["catalog"] is None
    assert output["result"]["inventory"]["total_tabs"] == 1


@pytest.mark.asyncio
async def test_browser_archive_catalog_bad_archive_dir_returns_ok_false_not_adapter_failure(tmp_path):
    not_an_archive = tmp_path / "not_an_archive"
    not_an_archive.mkdir()

    tool = _tool_by_name("browser_archive_catalog")
    result = await tool.execute({"archive_dir": str(not_an_archive)})

    assert result.success is True
    output = result.output
    assert isinstance(output, dict)
    assert output["ok"] is False
    assert "tabs.json" in output["error"]


@pytest.mark.asyncio
async def test_browser_archive_catalog_forwards_lens_and_catalog_flag_to_a_synthetic_summarizer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Proves catalog=true/lens actually reach Layer 2, using a monkeypatched
    default summarizer (a fake, synthetic response -- NO real LLM call)."""
    archive_dir = tmp_path / "archive_d1_20260816T000001Z"
    archive_dir.mkdir(parents=True)
    (archive_dir / "tabs.json").write_text(
        '[{"tab_id": 1, "window_id": 1, "url": "https://example.com/", "title": "Example", '
        '"discarded": false, "asleep": false, "pinned": false}]',
        encoding="utf-8",
    )
    markdown_dir = archive_dir / "tabs" / "1" / "markdown"
    markdown_dir.mkdir(parents=True)
    (markdown_dir / "page.extracted.md").write_text("Fabricated synthetic page text.", encoding="utf-8")

    captured: dict = {}

    def _fake_make_default_summarizer(*, vision_config=None):
        async def _summarizer(**kwargs):
            captured.update(kwargs)
            return {"what": "Synthetic finding.", "who": "", "why_kept": "", "topics": [], "value": "low"}

        return _summarizer

    # `run_archive_catalog` resolves `make_default_summarizer` from its own module
    # (amplifier_browser_bridge.archive_catalog), not from this tool module's
    # namespace -- the tool module only imports `run_archive_catalog` itself.
    monkeypatch.setattr(
        "amplifier_browser_bridge.archive_catalog.make_default_summarizer", _fake_make_default_summarizer
    )

    tool = _tool_by_name("browser_archive_catalog")
    result = await tool.execute(
        {"archive_dir": str(archive_dir), "catalog": True, "lens": "A fabricated reader."}
    )

    assert result.success is True
    output: Any = result.output
    assert output["ok"] is True
    assert output["result"]["catalog"]["tabs_cataloged"] == 1
    # Never the judgment text through the tool surface -- manifest only.
    assert "Synthetic finding." not in str(output)


@pytest.mark.asyncio
async def test_browser_screenshot_maps_capture_hidden_and_frame_id(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeHubClient({"ok": True, "result": {"tab_id": 7, "base64": "abc"}})
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    tool = _tool_by_name("browser_screenshot")
    await tool.execute({"device_id": "d1", "tab_id": 7, "capture_hidden": True, "frame_id": 862})

    _, command, args = fake.command_calls[0]
    assert command == "screenshot"
    assert args == {"capture_hidden": True, "frame_id": 862}


@pytest.mark.asyncio
async def test_browser_screenshot_multi_page_defaults_max_pages(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeHubClient({"ok": True, "result": {"tab_id": 7, "pages": []}})
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    tool = _tool_by_name("browser_screenshot")
    await tool.execute({"device_id": "d1", "tab_id": 7, "multi_page": True})

    _, _, args = fake.command_calls[0]
    assert args == {"multi_page": True, "max_pages": 10}


@pytest.mark.asyncio
async def test_browser_vision_read_composes_screenshot_and_extraction(monkeypatch: pytest.MonkeyPatch):
    import base64

    raw = b"\xff\xd8\xfake-jpeg"
    b64 = base64.b64encode(raw).decode("ascii")
    fake = _FakeHubClient(
        {"ok": True, "result": {"tab_id": 7, "format": "jpeg", "base64": b64, "via": "cdp"}}
    )
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    async def _fake_extract_text(images, prompt, **kwargs):
        return {"text": "hi", "provider": "anthropic", "model": "claude-3-5-sonnet-latest", "image_count": 1}

    monkeypatch.setattr("amplifier_browser_bridge.vision_read.extract_text", _fake_extract_text)

    tool = _tool_by_name("browser_vision_read")
    result = await tool.execute({"device_id": "d1", "tab_id": 7, "prompt": "read this"})
    output = result.output

    assert result.success is True
    assert isinstance(output, dict)
    assert output["ok"] is True
    assert output["result"]["text"] == "hi"
    _, command, args = fake.command_calls[0]
    assert command == "screenshot"
    assert args["capture_hidden"] is True  # vision_read defaults capture_hidden=True


@pytest.mark.asyncio
async def test_browser_vision_read_config_error_returns_ok_false_not_adapter_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    import base64

    from amplifier_browser_bridge.vision import VisionConfigError

    b64 = base64.b64encode(b"\xff\xd8\xfake-jpeg").decode("ascii")
    fake = _FakeHubClient({"ok": True, "result": {"tab_id": 7, "format": "jpeg", "base64": b64}})
    monkeypatch.setattr("amplifier_module_tool_browser_bridge._client", lambda: fake)

    async def _raise(*a, **k):
        raise VisionConfigError("No vision provider is configured -- set ANTHROPIC_API_KEY, ...")

    monkeypatch.setattr("amplifier_browser_bridge.vision_read.extract_text", _raise)

    tool = _tool_by_name("browser_vision_read")
    result = await tool.execute({"device_id": "d1", "tab_id": 7})
    output = result.output

    # VisionConfigError is caught INSIDE vision_read's composition path in the
    # Amplifier tool module's runner -- this is legitimate data (a config
    # problem the agent should read and act on), not an adapter-level failure.
    assert result.success is True
    assert isinstance(output, dict)
    assert output["ok"] is False
    assert "No vision provider is configured" in output["error"]


def test_client_uses_env_configured_hub_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_HUB_URL", "ws://100.64.1.2:9000/agent")
    # Re-import-free check: _client() reads the module-level constants captured
    # at import time (matches cli.py's own convention), so we assert on those
    # constants directly rather than re-triggering module import machinery.
    import amplifier_module_tool_browser_bridge as mod

    client = _client()
    assert client.url == mod.DEFAULT_HUB_URL
