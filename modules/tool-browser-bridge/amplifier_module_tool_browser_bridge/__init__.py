"""Amplifier tool module: browser-bridge.

Wraps `amplifier_browser_bridge` (the Python lib -- the single home for all logic,
see the repo root's client.py/addressing.py) as Amplifier-callable tools. Same tool
vocabulary as `mcp_server.py`, for consistency across both agent surfaces (design
doc section 3.3): one Amplifier tool per browser-bridge command, each a thin
wrapper over `HubClient`. No policy or business logic lives here -- every tool's
`execute()` does nothing but build a `Target`, call the lib, and hand the hub's
response straight back (including `{"status": "queued", ...}` for a non-live
device -- see `_HubTool.execute` for the one place that pass-through is
guaranteed).

`HubClient` is already async (it awaits a websocket round-trip), so tools call it
directly with `await` -- no `asyncio.to_thread` needed; that's only for wrapping
genuinely blocking/synchronous code, which nothing here is.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from amplifier_browser_bridge import HubClient, HubError, Target
from amplifier_core import ToolResult

logger = logging.getLogger(__name__)

DEFAULT_HUB_URL = os.environ.get("ABB_HUB_URL", "ws://127.0.0.1:8900/agent")
DEFAULT_TOKEN = os.environ.get("ABB_TOKEN")

# Repeated verbatim in every tab-acting tool's description below -- see
# mcp_server.py's module docstring for why this is plain repeated text rather
# than a string spliced onto multiple docstrings.
_QUEUE_NOTE = (
    "If the device is not 'live', this returns immediately as "
    '{"status": "queued", "command_id": ..., "tier": ..., "last_seen": ..., '
    '"queue_position": ...} instead of {"ok": ...}. That is a normal, actionable '
    "result, not an error or a hang -- call browser_poll(device_id, command_id) "
    "later to retrieve the eventual result."
)


def _client() -> HubClient:
    return HubClient(DEFAULT_HUB_URL, token=DEFAULT_TOKEN)


Runner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class _HubTool:
    """Generic thin Amplifier tool: one instance per browser-bridge command.

    Holds no command-specific logic of its own -- it exists only to satisfy the
    Tool protocol (name/description/input_schema/execute) around whatever
    `runner` coroutine it's constructed with. Each `runner` below is a small
    function that maps `input_data` to one `HubClient` call.
    """

    def __init__(self, name: str, description: str, input_schema: dict[str, Any], runner: Runner) -> None:
        self._name = name
        self._description = description
        self._input_schema = input_schema
        self._runner = runner

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._input_schema

    async def execute(self, input_data: dict[str, Any]) -> ToolResult:
        """Run the command and pass the hub's response straight back as output.

        This is the one place the tier pass-through guarantee lives for this
        surface: whatever dict the hub returned (ok/result, ok/error, or
        status=queued/tier/...) becomes `ToolResult(success=True, output=<that
        dict>)` verbatim. `success=False` is reserved for adapter-level failures
        (a HubError -- e.g. the hub itself is unreachable), not for `ok: false`
        command results, which are legitimate data the calling agent must see.
        """
        try:
            result = await self._runner(input_data)
        except HubError as e:
            return ToolResult(success=False, output=f"hub error: {e}")
        return ToolResult(success=True, output=result)


def _target(input_data: dict[str, Any]) -> Target:
    return Target(
        device_id=input_data["device_id"],
        window_id=input_data.get("window_id"),
        tab_id=input_data.get("tab_id"),
    )


async def _command(
    command: str, args_fn: Callable[[dict[str, Any]], dict[str, Any]], input_data: dict[str, Any]
) -> dict[str, Any]:
    return await _client().command(_target(input_data), command, args_fn(input_data))


def _no_args(_input_data: dict[str, Any]) -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# One (name, description, input_schema, runner) tuple per browser-bridge
# command. Descriptions and schemas mirror mcp_server.py's tool set exactly, so
# an agent sees the same vocabulary regardless of which surface it's using.
# ---------------------------------------------------------------------------

_DEVICE_ID_PROP = {"device_id": {"type": "string", "description": "Device id, from browser_devices."}}
_TAB_TARGET_PROPS = {
    **_DEVICE_ID_PROP,
    "tab_id": {"type": "integer", "description": "Tab id, from browser_tabs."},
    "window_id": {"type": "integer", "description": "Optional window id (disambiguates reused tab ids)."},
}


def _build_tools() -> list[_HubTool]:
    async def devices_runner(_input_data: dict[str, Any]) -> dict[str, Any]:
        devices = await _client().list_devices()
        return {"ok": True, "devices": devices}

    async def poll_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        return await _client().poll(input_data["device_id"], input_data["command_id"])

    async def tabs_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        return await _client().command(
            Target(device_id=input_data["device_id"], window_id=input_data.get("window_id")), "tabs", {}
        )

    async def tab_open_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        return await _client().command(
            Target(device_id=input_data["device_id"]),
            "tab_open",
            {"url": input_data.get("url", "about:blank"), "active": input_data.get("active", False)},
        )

    def click_args(input_data: dict[str, Any]) -> dict[str, Any]:
        return {"ref": input_data["ref"]}

    def type_args(input_data: dict[str, Any]) -> dict[str, Any]:
        return {"ref": input_data["ref"], "text": input_data["text"]}

    def key_args(input_data: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {"key": input_data["key"]}
        if input_data.get("ref") is not None:
            args["ref"] = input_data["ref"]
        return args

    def scroll_args(input_data: dict[str, Any]) -> dict[str, Any]:
        return {"x": input_data.get("x", 0), "y": input_data.get("y", 0)}

    def navigate_args(input_data: dict[str, Any]) -> dict[str, Any]:
        return {"url": input_data["url"]}

    def wait_for_args(input_data: dict[str, Any]) -> dict[str, Any]:
        return {"selector": input_data["selector"], "timeout_ms": input_data.get("timeout_ms", 10000)}

    def wait_text_args(input_data: dict[str, Any]) -> dict[str, Any]:
        return {"text": input_data["text"], "timeout_ms": input_data.get("timeout_ms", 10000)}

    return [
        _HubTool(
            "browser_devices",
            "List every known browser device: id, label, platform, connectivity tier "
            "(live/intermittent/dormant), behaviorally-probed capabilities (e.g. whether CDP/debugger "
            "or background-tab screenshot is available), and current queue length. ALWAYS call this "
            "first -- it is the entry point for addressing every other tool.",
            {"type": "object", "properties": {}},
            devices_runner,
        ),
        _HubTool(
            "browser_tabs",
            "List open tabs on a device, optionally scoped to one window_id. Use this after "
            "browser_devices() to discover tab_id values for the other tools. " + _QUEUE_NOTE,
            {"type": "object", "properties": _DEVICE_ID_PROP, "required": ["device_id"]},
            tabs_runner,
        ),
        _HubTool(
            "browser_snapshot",
            "Accessibility-style snapshot of a tab: a tree of elements with stable `ref` ids (e.g. "
            "'e12') you can pass to browser_click/browser_type/browser_key. Refs reset on navigation "
            "-- take a fresh snapshot after navigating. " + _QUEUE_NOTE,
            {"type": "object", "properties": _TAB_TARGET_PROPS, "required": ["device_id", "tab_id"]},
            lambda input_data: _command("snapshot", _no_args, input_data),
        ),
        _HubTool(
            "browser_read",
            "Read the full visible text of a tab. " + _QUEUE_NOTE,
            {"type": "object", "properties": _TAB_TARGET_PROPS, "required": ["device_id", "tab_id"]},
            lambda input_data: _command("read", _no_args, input_data),
        ),
        _HubTool(
            "browser_click",
            "Click an element by ref (from a prior browser_snapshot call). " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {**_TAB_TARGET_PROPS, "ref": {"type": "string", "description": "Element ref."}},
                "required": ["device_id", "tab_id", "ref"],
            },
            lambda input_data: _command("click", click_args, input_data),
        ),
        _HubTool(
            "browser_type",
            "Type text into an element by ref (from a prior browser_snapshot call). " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_TAB_TARGET_PROPS,
                    "ref": {"type": "string", "description": "Element ref."},
                    "text": {"type": "string", "description": "Text to type."},
                },
                "required": ["device_id", "tab_id", "ref", "text"],
            },
            lambda input_data: _command("type", type_args, input_data),
        ),
        _HubTool(
            "browser_key",
            "Send a key press (e.g. 'Enter', 'Escape', 'Tab'), optionally focused on a specific "
            "element ref first. " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_TAB_TARGET_PROPS,
                    "key": {"type": "string", "description": "Key name, e.g. 'Enter'."},
                    "ref": {"type": "string", "description": "Optional element ref to focus first."},
                },
                "required": ["device_id", "tab_id", "key"],
            },
            lambda input_data: _command("key", key_args, input_data),
        ),
        _HubTool(
            "browser_scroll",
            "Scroll a tab to absolute coordinates (x, y). " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_TAB_TARGET_PROPS,
                    "x": {"type": "integer", "default": 0},
                    "y": {"type": "integer", "default": 0},
                },
                "required": ["device_id", "tab_id"],
            },
            lambda input_data: _command("scroll", scroll_args, input_data),
        ),
        _HubTool(
            "browser_navigate",
            "Navigate a tab to a URL. " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {**_TAB_TARGET_PROPS, "url": {"type": "string"}},
                "required": ["device_id", "tab_id", "url"],
            },
            lambda input_data: _command("navigate", navigate_args, input_data),
        ),
        _HubTool(
            "browser_tab_open",
            "Open a new tab on a device. No tab_id exists yet, so target is device-only. `active` "
            "defaults to false (co-working etiquette: don't steal focus) -- the new tab opens in the "
            "background unless active=true is explicitly requested. " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_DEVICE_ID_PROP,
                    "url": {"type": "string", "default": "about:blank"},
                    "active": {"type": "boolean", "default": False},
                },
                "required": ["device_id"],
            },
            tab_open_runner,
        ),
        _HubTool(
            "browser_tab_close",
            "Close a tab. " + _QUEUE_NOTE,
            {"type": "object", "properties": _TAB_TARGET_PROPS, "required": ["device_id", "tab_id"]},
            lambda input_data: _command("tab_close", _no_args, input_data),
        ),
        _HubTool(
            "browser_tab_activate",
            "Bring a tab to the foreground. This is the one command explicitly allowed to steal "
            "focus, because it was asked to -- prefer acting on background tabs wherever a command "
            "allows it (co-working etiquette, design doc section 6.3). " + _QUEUE_NOTE,
            {"type": "object", "properties": _TAB_TARGET_PROPS, "required": ["device_id", "tab_id"]},
            lambda input_data: _command("tab_activate", _no_args, input_data),
        ),
        _HubTool(
            "browser_screenshot",
            "Screenshot a tab. In this injection-only phase (no CDP yet), this only succeeds if the "
            "target tab is already the active tab of a focused window -- it fails loud rather than "
            "silently activating the tab to comply. Check browser_devices()'s "
            "capabilities.capture_visible_tab first; on Android this is the ONLY way to see a tab, "
            "and only ever the currently-active one (design doc section 7). " + _QUEUE_NOTE,
            {"type": "object", "properties": _TAB_TARGET_PROPS, "required": ["device_id", "tab_id"]},
            lambda input_data: _command("screenshot", _no_args, input_data),
        ),
        _HubTool(
            "browser_wait_for",
            "Poll (never sleep blindly) until a CSS selector matches an element, or time out after "
            "timeout_ms. " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_TAB_TARGET_PROPS,
                    "selector": {"type": "string"},
                    "timeout_ms": {"type": "integer", "default": 10000},
                },
                "required": ["device_id", "tab_id", "selector"],
            },
            lambda input_data: _command("wait_for", wait_for_args, input_data),
        ),
        _HubTool(
            "browser_wait_text",
            "Poll (never sleep blindly) until the tab's visible text contains a substring, or time "
            "out after timeout_ms. " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_TAB_TARGET_PROPS,
                    "text": {"type": "string"},
                    "timeout_ms": {"type": "integer", "default": 10000},
                },
                "required": ["device_id", "tab_id", "text"],
            },
            lambda input_data: _command("wait_text", wait_text_args, input_data),
        ),
        _HubTool(
            "browser_poll",
            "Check on (or retrieve the eventual result of) a command that was previously reported as "
            'queued. Returns one of three shapes: {"status": "queued", "queue_position": ..., '
            '"tier": ...} if still waiting for the device, {"status": "pending"} if the device is '
            'live and executing it right now, or the final {"ok": ...} result once it has actually '
            "run.",
            {
                "type": "object",
                "properties": {
                    **_DEVICE_ID_PROP,
                    "command_id": {"type": "string", "description": "command_id from a queued result."},
                },
                "required": ["device_id", "command_id"],
            },
            poll_runner,
        ),
    ]


async def mount(coordinator: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mount every browser-bridge tool into the coordinator (the mount() Iron Law:
    each tool is registered via `coordinator.mount("tools", tool, name=tool.name)`).
    """
    tools = _build_tools()
    for tool in tools:
        await coordinator.mount("tools", tool, name=tool.name)
    logger.info("tool-browser-bridge mounted: registered %d tools", len(tools))
    return {
        "name": "tool-browser-bridge",
        "version": "0.1.0",
        "provides": [tool.name for tool in tools],
    }
