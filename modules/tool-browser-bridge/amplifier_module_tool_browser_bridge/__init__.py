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

See docs/DECISION_GUIDE.md in the amplifier-browser-bridge repo for WHICH of these
tools to reach for and when -- a dozen read/act mechanisms plus modifiers (wake,
activate, trusted, capture_hidden) is real power with no map otherwise. This module
picks nothing for you; it forwards exactly what the caller asked for.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from amplifier_core import ToolResult

from ._stale_install_guard import reraise_with_diagnosis

# Kept as a real, static `from ... import ...` (wrapped in try/except rather than
# replaced with a dynamic importlib lookup) so pyright still types HubClient/HubError/
# Target as their actual classes -- used below in annotations (`-> Target:`) and an
# `except HubError:` clause, neither of which would type-check against a generic
# `type` returned from a helper function.
#
# A stale editable-install pointer (e.g. after an Amplifier cache reset, or the repo's
# clone URL moving) leaves `amplifier_browser_bridge` resolving as an empty
# namespace-package shadow instead of the real module; a bare import here then fails
# with a cryptic "cannot import name 'HubClient' from 'amplifier_browser_bridge'
# (unknown location)" that names a missing class instead of the actual dead install
# pointer. `reraise_with_diagnosis` (see its own docstring) detects that specific shape
# and raises `StaleEditableInstallError` naming what was actually found instead.
try:
    from amplifier_browser_bridge import HubClient, HubError, Target
except ImportError as _import_exc:
    reraise_with_diagnosis(_import_exc)

from amplifier_browser_bridge.auth import resolve_default_token
from amplifier_browser_bridge.hub_location import resolve_hub_url
from amplifier_browser_bridge.vision import VisionConfigError, VisionError
from amplifier_browser_bridge.vision_read import vision_read

logger = logging.getLogger(__name__)

# Resolution order (env var > persisted hub location from `amplifier-browser-bridge
# init`/`service install` > loopback fallback) -- see hub_location.py's module
# docstring. Before this fix, this constant hardcoded the loopback fallback
# independently of cli.py/mcp_server.py's own copies of the same literal --
# one of the four call sites that could silently disagree with where `init`
# actually told the user the hub was.
DEFAULT_HUB_URL = resolve_hub_url()
# Same fix, applied to auth: falls back to the token file's `default` entry
# when no env var is set (auth.py's `resolve_default_token`).
DEFAULT_TOKEN = resolve_default_token()

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
    args = args_fn(input_data)
    # `timeout_s`, if the caller supplied one, overrides the hub's default
    # device-round-trip wait for just this call (see hub.py's
    # DEFAULT_COMMAND_TIMEOUT / protocol.py's HUB_ONLY_ARGS) -- surfaced
    # uniformly here rather than in every individual args_fn, since it applies
    # identically to every tab-targeting command. See `_TAB_TARGET_PROPS`.
    timeout_s = input_data.get("timeout_s")
    if timeout_s is not None:
        args = {**args, "timeout_s": timeout_s}
    # `session_id`, if the caller supplied one, must come from a prior
    # browser_establish_session call -- the hub enforces that session's
    # declared write scope (docs/designs/confirmation-gate.md section 11.2)
    # against STATE_CHANGING_COMMANDS (click/type/key/navigate) before they
    # reach the device. Harmless to pass on read-only commands too; the hub
    # only consults it for state-changing ones.
    session_id = input_data.get("session_id")
    return await _client().command(_target(input_data), command, args, session_id=session_id)


def _no_args(_input_data: dict[str, Any]) -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# One (name, description, input_schema, runner) tuple per browser-bridge
# command. Descriptions and schemas mirror mcp_server.py's tool set exactly, so
# an agent sees the same vocabulary regardless of which surface it's using.
# ---------------------------------------------------------------------------

_DEVICE_ID_PROP = {"device_id": {"type": "string", "description": "Device id, from browser_devices."}}
_SESSION_ID_PROP = {
    "session_id": {
        "type": "string",
        "description": (
            "Optional session id from a prior browser_establish_session call. If given, the hub "
            "enforces that session's declared write scope (docs/designs/confirmation-gate.md "
            "section 11.2) against this command before it reaches the device."
        ),
    }
}
_TAB_TARGET_PROPS = {
    **_DEVICE_ID_PROP,
    "tab_id": {"type": "integer", "description": "Tab id, from browser_tabs."},
    "window_id": {"type": "integer", "description": "Optional window id (disambiguates reused tab ids)."},
    "timeout_s": {
        "type": "number",
        "description": (
            "Optional override of the hub's default device-round-trip wait, in seconds, for this "
            "command only -- useful for a heavy/slow-hydrating page (see docs/PROTOCOL.md's "
            "'Command timeout' section)."
        ),
    },
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

    async def reload_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        return await _client().command(Target(device_id=input_data["device_id"]), "reload", {})

    def read_or_snapshot_args(input_data: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if input_data.get("wake"):
            args["wake"] = True
        if input_data.get("activate"):
            args["activate"] = True
        return args

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

    async def fetch_bytes_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {"url": input_data["url"]}
        if input_data.get("max_bytes") is not None:
            args["max_bytes"] = input_data["max_bytes"]
        return await _client().command(Target(device_id=input_data["device_id"]), "fetch_bytes", args)

    async def downloads_list_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        return await _client().command(
            Target(device_id=input_data["device_id"]),
            "downloads_list",
            {"limit": input_data.get("limit", 20)},
        )

    async def download_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {"url": input_data["url"]}
        if input_data.get("filename") is not None:
            args["filename"] = input_data["filename"]
        return await _client().command(Target(device_id=input_data["device_id"]), "download", args)

    async def wait_download_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        if input_data.get("download_id") is None and input_data.get("since_id") is None:
            return {"ok": False, "error": "browser_wait_download requires download_id or since_id"}
        args: dict[str, Any] = {"timeout_ms": input_data.get("timeout_ms", 30000)}
        if input_data.get("download_id") is not None:
            args["download_id"] = input_data["download_id"]
        if input_data.get("since_id") is not None:
            args["since_id"] = input_data["since_id"]
        if input_data.get("pattern") is not None:
            args["pattern"] = input_data["pattern"]
        return await _client().command(Target(device_id=input_data["device_id"]), "wait_download", args)

    def grab_image_args(input_data: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {"url": input_data["url"]}
        if input_data.get("max_bytes") is not None:
            args["max_bytes"] = input_data["max_bytes"]
        return args

    def screenshot_args(input_data: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if input_data.get("capture_hidden"):
            args["capture_hidden"] = True
        if input_data.get("frame_id") is not None:
            args["frame_id"] = input_data["frame_id"]
        if input_data.get("multi_page"):
            args["multi_page"] = True
            args["max_pages"] = input_data.get("max_pages", 10)
        if input_data.get("scroll_selector") is not None:
            args["scroll_selector"] = input_data["scroll_selector"]
        if input_data.get("page_delay_ms") is not None:
            args["page_delay_ms"] = input_data["page_delay_ms"]
        return args

    async def establish_session_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        read = input_data.get("read", "*")
        write = input_data.get("write", "*")
        return await _client().establish_session(
            read=read if read == "*" else [o.strip() for o in str(read).split(",") if o.strip()],
            write=write if write == "*" else [o.strip() for o in str(write).split(",") if o.strip()],
            on_unknown=input_data.get("on_unknown", "allow"),
            redeem=input_data.get("redeem", "agent"),
            unattended=bool(input_data.get("unattended", False)),
        )

    async def narrow_scope_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if input_data.get("write") is not None:
            kwargs["write"] = [o.strip() for o in str(input_data["write"]).split(",") if o.strip()]
        if input_data.get("read") is not None:
            kwargs["read"] = [o.strip() for o in str(input_data["read"]).split(",") if o.strip()]
        if input_data.get("on_unknown") is not None:
            kwargs["on_unknown"] = input_data["on_unknown"]
        if input_data.get("redeem") is not None:
            kwargs["redeem"] = input_data["redeem"]
        if input_data.get("unattended"):
            kwargs["unattended"] = True
        return await _client().narrow_scope(input_data["session_id"], **kwargs)

    async def vision_read_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        target = _target(input_data)
        try:
            return await vision_read(
                _client(),
                target,
                prompt=input_data.get("prompt"),
                frame_id=input_data.get("frame_id"),
                multi_page=bool(input_data.get("multi_page", False)),
                max_pages=input_data.get("max_pages"),
                scroll_selector=input_data.get("scroll_selector"),
                page_delay_ms=input_data.get("page_delay_ms"),
                capture_hidden=bool(input_data.get("capture_hidden", True)),
                timeout_s=input_data.get("timeout_s"),
            )
        except (VisionConfigError, VisionError) as e:
            return {"ok": False, "error": str(e)}

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
            "Accessibility-style snapshot of a tab: a tree of elements with stable, frame-qualified "
            "`ref` ids (e.g. 'f0.e12') you can pass to browser_click/browser_type/browser_key. Each "
            "node also carries a `generation` -- refs are only valid from the MOST RECENT snapshot of "
            "a given frame; using a ref from a superseded snapshot fails loud with a specific 'stale "
            "ref' error rather than silently doing nothing. Refs reset on navigation -- take a fresh "
            "snapshot after navigating. At real-world scale (hundreds of tabs) Edge discards "
            "background tabs to reclaim memory; a discarded tab fails loud naming the real cause "
            "(check browser_tabs()'s `discarded` field). Pass wake=true to reload a discarded tab and "
            "retry -- destroys in-page state, so opt-in only; result reports 'woke': true. A heavy/"
            "hydrated SPA can be slow or time out while the tab is NOT active -- pass activate=true to "
            "foreground it first (never automatic; steals focus; result reports 'activated': true). "
            + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_TAB_TARGET_PROPS,
                    "wake": {"type": "boolean", "default": False},
                    "activate": {"type": "boolean", "default": False},
                },
                "required": ["device_id", "tab_id"],
            },
            lambda input_data: _command("snapshot", read_or_snapshot_args, input_data),
        ),
        _HubTool(
            "browser_read",
            "Read the full visible text of a tab. At real-world scale (hundreds of tabs) Edge "
            "discards background tabs to reclaim memory; a discarded tab fails loud naming the real "
            "cause (check browser_tabs()'s `discarded` field). Pass wake=true to reload a discarded "
            "tab and retry -- destroys in-page state, so opt-in only; result reports 'woke': true. A "
            "heavy/hydrated SPA can be slow or time out while the tab is NOT active -- pass "
            "activate=true to foreground it first (never automatic; steals focus; result reports "
            "'activated': true). " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_TAB_TARGET_PROPS,
                    "wake": {"type": "boolean", "default": False},
                    "activate": {"type": "boolean", "default": False},
                },
                "required": ["device_id", "tab_id"],
            },
            lambda input_data: _command("read", read_or_snapshot_args, input_data),
        ),
        _HubTool(
            "browser_click",
            "Click an element by ref (from a prior browser_snapshot call). " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_TAB_TARGET_PROPS,
                    **_SESSION_ID_PROP,
                    "ref": {"type": "string", "description": "Element ref."},
                },
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
                    **_SESSION_ID_PROP,
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
                    **_SESSION_ID_PROP,
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
                "properties": {**_TAB_TARGET_PROPS, **_SESSION_ID_PROP, "url": {"type": "string"}},
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
            "browser_reload",
            "Reload the extension on a device (chrome.runtime.reload()). Self-service for "
            "unpacked-extension iteration: after updating extension/ files on the device's "
            "machine, this picks up the change without a manual click in edge://extensions. "
            "Note: the very first deployment of this command itself still requires one manual "
            "reload -- an extension has to already understand `reload` before it can reload "
            "itself into a version that understands it.",
            {"type": "object", "properties": _DEVICE_ID_PROP, "required": ["device_id"]},
            reload_runner,
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
            "Screenshot a tab -- returns PIXELS (base64 + format), no model call. This is the "
            "'return pixels' mechanism: if you (the calling agent) can see images directly, this is "
            "the cheapest and most faithful option. If you need TEXT extracted from the image "
            "instead (e.g. you can't process images, or want OCR'd text in context), use "
            "browser_vision_read instead -- a distinct, explicitly different mechanism that makes a "
            "real vision-model API call; this tool never does that. capture_hidden=true captures a "
            "tab that is NOT the active tab of a focused window (auto-escalates to CDP; requires the "
            "debugger capability -- check browser_devices()'s capabilities.debugger first); without "
            "it, only the active tab of a focused window can be captured, and this fails loud rather "
            "than silently activating the tab. frame_id crops the capture to one frame's own "
            "on-screen region (from a prior browser_snapshot/browser_read's `frames` entries) -- "
            "requires capture_hidden. multi_page=true scrolls and captures repeatedly (up to "
            "max_pages, default 10, hard cap 50) until the scrollable region's end is reached -- for "
            "content that doesn't fit one viewport (e.g. a multi-page document viewer); returns a "
            "`pages` array plus honest `capped`/`stopped_reason` metadata (never silently returns a "
            "partial result as if it were complete). scroll_selector targets a specific scrollable "
            "container (CSS selector); page_delay_ms is the settle delay between scroll and capture. "
            "On Android (no CDP), only the active tab can ever be captured. " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_TAB_TARGET_PROPS,
                    "capture_hidden": {"type": "boolean", "default": False},
                    "frame_id": {"type": "integer", "description": "Crop to this frame's on-screen region."},
                    "multi_page": {"type": "boolean", "default": False},
                    "max_pages": {
                        "type": "integer",
                        "default": 10,
                        "description": "Cap for multi_page (max 50).",
                    },
                    "scroll_selector": {"type": "string", "description": "CSS selector of scroll container."},
                    "page_delay_ms": {
                        "type": "integer",
                        "description": "Settle delay between scroll and capture.",
                    },
                },
                "required": ["device_id", "tab_id"],
            },
            lambda input_data: _command("screenshot", screenshot_args, input_data),
        ),
        _HubTool(
            "browser_vision_read",
            "Capture pixels and extract TEXT from them via a vision-capable LLM -- a real, separate "
            "model-call mechanism, distinct from browser_screenshot (which only returns pixels and "
            "never calls a model). Use this when the content you need was never present in the DOM "
            "as text (e.g. a canvas-rendered document viewer, like Word/PowerPoint Online) and you "
            "want text back rather than an image. Requires a vision provider configured via "
            "environment variable on the machine running this hub/tool (ANTHROPIC_API_KEY / "
            "OPENAI_API_KEY / GOOGLE_API_KEY, or AMPLIFIER_BROWSER_BRIDGE_VISION_PROVIDER to pin one) -- fails loud with "
            "setup instructions ({'ok': false, 'error': ...}) if none is configured; never silently "
            "returns empty text. capture_hidden defaults to true here (unlike browser_screenshot) -- "
            "this tool exists specifically to reach tabs you shouldn't activate just to look at. "
            "frame_id/multi_page/max_pages/scroll_selector/page_delay_ms mean exactly what they mean "
            "on browser_screenshot. Returns {'ok': true, 'result': {'text': ..., 'vision_provider': "
            "..., 'vision_model': ..., 'image_count': ..., 'page_count': ..., 'capped': ..., "
            "'stopped_reason': ...}} on success, or the hub's own queued/error shape if the "
            "underlying capture itself was queued or failed (the vision model is never called "
            "without a real captured image in hand). " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_TAB_TARGET_PROPS,
                    "prompt": {"type": "string", "description": "What to extract/ask about the image(s)."},
                    "frame_id": {"type": "integer", "description": "Crop to this frame's on-screen region."},
                    "multi_page": {"type": "boolean", "default": False},
                    "max_pages": {
                        "type": "integer",
                        "default": 10,
                        "description": "Cap for multi_page (max 50).",
                    },
                    "scroll_selector": {"type": "string", "description": "CSS selector of scroll container."},
                    "page_delay_ms": {
                        "type": "integer",
                        "description": "Settle delay between scroll and capture.",
                    },
                    "capture_hidden": {"type": "boolean", "default": True},
                },
                "required": ["device_id", "tab_id"],
            },
            vision_read_runner,
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
            "browser_fetch_bytes",
            "Fetch a URL from the EXTENSION's own context, with credentials included -- rides the "
            "user's real authenticated session (cookies) for the target origin. No tab_id needed. "
            "This is the mechanism for retrieving a linked file (.docx/.pdf/binary) that a page only "
            "links to, using the user's existing login -- distinct from browser_read/browser_snapshot, "
            "which only ever see text already present in the DOM (a canvas-rendered document, e.g. a "
            "Word Online viewer, has NO document text in the DOM at all -- this is the way to get its "
            "content). Returns {url, content_type, byte_length, base64} on success. Refuses (naming the "
            "limit) past a byte-size cap (default 25MB) -- pass max_bytes to raise it. If the target "
            "blocks extension-context requests (some CDNs/hotlink protection check the request's "
            "Referer/Origin), browser_grab_image fetches from the PAGE's own script context instead. "
            + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_DEVICE_ID_PROP,
                    "url": {"type": "string"},
                    "max_bytes": {"type": "integer", "description": "Override the default byte-size cap."},
                },
                "required": ["device_id", "url"],
            },
            fetch_bytes_runner,
        ),
        _HubTool(
            "browser_grab_image",
            "Fetch a URL from the PAGE's own main-world script context (not the extension's) -- the "
            "request carries the page's own Referer and cookie context, defeating hotlink/Referer "
            "protection an extension-context fetch (browser_fetch_bytes) would trip. Requires a tab_id "
            "(the page whose script context does the fetching). Use this specifically when "
            "browser_fetch_bytes fails with an HTTP error, or you already know the target needs the "
            "page's own session context. Returns {url, content_type, byte_length, base64} on success; "
            "refuses (naming the limit) past a byte-size cap (default 25MB). " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_TAB_TARGET_PROPS,
                    "url": {"type": "string"},
                    "max_bytes": {"type": "integer", "description": "Override the default byte-size cap."},
                },
                "required": ["device_id", "tab_id", "url"],
            },
            lambda input_data: _command("grab_image", grab_image_args, input_data),
        ),
        _HubTool(
            "browser_downloads_list",
            "List recent downloads on a device (chrome.downloads.search), plus max_download_id -- the "
            "highest download id chrome currently knows about. Call this BEFORE an action that "
            "triggers a native/indirect download (e.g. clicking a page's own Download control) and "
            "pass its max_download_id as browser_wait_download's since_id, so the new download is "
            "identified without ever mistaking one the human started themselves for the agent's own. "
            + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {**_DEVICE_ID_PROP, "limit": {"type": "integer", "default": 20}},
                "required": ["device_id"],
            },
            downloads_list_runner,
        ),
        _HubTool(
            "browser_download",
            "Trigger a download of a URL directly (chrome.downloads.download) -- returns a download_id "
            "you already know precisely, since this command started the download itself. Pass it to "
            "browser_wait_download's download_id to poll for completion. " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_DEVICE_ID_PROP,
                    "url": {"type": "string"},
                    "filename": {"type": "string", "description": "Suggested filename for the download."},
                },
                "required": ["device_id", "url"],
            },
            download_runner,
        ),
        _HubTool(
            "browser_wait_download",
            "Poll (never sleep blindly) for a completed download. Exactly one of download_id (from a "
            "prior browser_download call) or since_id (a baseline max_download_id from "
            "browser_downloads_list, taken BEFORE an action that triggers an indirect download) is "
            "required -- since_id mode never matches a download at or below the baseline, so it "
            "structurally cannot claim a download the human started themselves. pattern (optional "
            "regex) narrows a since_id search by filename. Returns {download_id, filename, url, mime, "
            "byte_length, state} once complete, or an error if the download was interrupted or the "
            "timeout_ms deadline passed first. " + _QUEUE_NOTE,
            {
                "type": "object",
                "properties": {
                    **_DEVICE_ID_PROP,
                    "download_id": {"type": "integer"},
                    "since_id": {"type": "integer", "description": "Baseline max_download_id."},
                    "pattern": {"type": "string", "description": "Optional regex on the filename."},
                    "timeout_ms": {"type": "integer", "default": 30000},
                },
                "required": ["device_id"],
            },
            wait_download_runner,
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
        _HubTool(
            "browser_establish_session",
            "Create a new session with a caller-declared WRITE scope "
            "(docs/designs/confirmation-gate.md, Candidate C) -- a boundary the page itself can never "
            "touch. Pass the returned session_id to browser_click/browser_type/browser_key/"
            "browser_navigate to enforce this scope on those commands. ALWAYS creates a brand-new "
            "session with a fresh session_id -- can never be used to reset an existing session's scope "
            "back to broad. To change an existing session, use browser_narrow_scope instead, which can "
            "only ever narrow, never widen.",
            {
                "type": "object",
                "properties": {
                    "write": {
                        "type": "string",
                        "default": "*",
                        "description": (
                            "'*' (default, unrestricted) or comma-separated hostnames (subdomain-inclusive)."
                        ),
                    },
                    "read": {"type": "string", "default": "*"},
                    "on_unknown": {
                        "type": "string",
                        "enum": ["allow", "gate", "deny"],
                        "default": "allow",
                    },
                    "redeem": {"type": "string", "enum": ["agent", "unredeemable"], "default": "agent"},
                    "unattended": {"type": "boolean", "default": False},
                },
            },
            establish_session_runner,
        ),
        _HubTool(
            "browser_narrow_scope",
            "Narrow an EXISTING session's scope -- NEVER widens (docs/designs/confirmation-gate.md "
            "section 11.2). write/read may only shrink to a strict subset of the current grant, "
            "on_unknown may only move allow -> gate -> deny, redeem only agent -> unredeemable, "
            "unattended only False -> True. Only the parameters you pass are touched. Once the "
            "session has ingested any page content (a browser_read/browser_snapshot/browser_tabs "
            "result), the hub SEALS it and every subsequent call -- including this one -- is rejected "
            "outright, no matter how narrow the request.",
            {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "write": {"type": "string", "description": "Comma-separated hostnames to narrow to."},
                    "read": {"type": "string", "description": "Comma-separated hostnames to narrow to."},
                    "on_unknown": {"type": "string", "enum": ["allow", "gate", "deny"]},
                    "redeem": {"type": "string", "enum": ["agent", "unredeemable"]},
                    "unattended": {"type": "boolean", "default": False},
                },
                "required": ["session_id"],
            },
            narrow_scope_runner,
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
