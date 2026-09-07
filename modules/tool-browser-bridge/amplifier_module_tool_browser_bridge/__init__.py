"""Amplifier tool module: browser-bridge.

Wraps `amplifier_browser_bridge` (the Python lib -- the single home for all logic,
see the repo root's client.py/addressing.py) as Amplifier-callable tools. No policy
or business logic lives here -- every operation's runner does nothing but build a
`Target`, call the lib, and hand the hub's response straight back (including
`{"status": "queued", ...}` for a non-live device -- see `_HubTool.execute` for the
one place that pass-through is guaranteed).

SEVEN tools, not one per command. Every mounted tool's name + description +
input_schema is serialised into the request on EVERY request of EVERY session that
mounts this bundle, called or not -- so the tool surface is a permanent per-request
cost, not a documentation surface. This module used to mount 31 flat tools, one per
browser-bridge command; it now mounts seven subject-area tools with an `operation`
enum inside each, the shape `android_inspector`/`ios_inspector`/`terminal_inspector`
already establish. Shared parameters (device_id/tab_id/window_id/timeout_s) are
declared once per tool instead of once per command.

Nothing was deleted in that move: `LEGACY_TOOLS` below maps every one of the 31
former tool names to exactly one `(tool, operation)` pair, and
`tests/test_consolidated_surface.py` fails if any of them stops resolving.

This surface therefore NO LONGER matches `mcp_server.py`'s vocabulary, which still
mounts 29 flat tools. That divergence is deliberate and recorded in
docs/AGENT_SURFACES.md; the MCP server was left unedited by this change.

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
from typing import Any, NamedTuple

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

from amplifier_browser_bridge.archive import DEFAULT_DEPTH, ArchiveError
from amplifier_browser_bridge.archive import run_archive as _run_archive
from amplifier_browser_bridge.archive_catalog import DEFAULT_CONCURRENCY, DEFAULT_TOP_N, CatalogError
from amplifier_browser_bridge.archive_catalog import run_archive_catalog as _run_archive_catalog
from amplifier_browser_bridge.archive_convert import ConversionError
from amplifier_browser_bridge.archive_convert import run_archive_convert as _run_archive_convert
from amplifier_browser_bridge.auth import resolve_default_token
from amplifier_browser_bridge.auto_setup import DEFAULT_WAIT_REACHABLE_S, run_auto_setup
from amplifier_browser_bridge.doctor import run_doctor
from amplifier_browser_bridge.hub_location import DEFAULT_PORT, resolve_hub_url
from amplifier_browser_bridge.paging import DEFAULT_LIMIT, shape_tabs_response
from amplifier_browser_bridge.update_extension import DEFAULT_RECONNECT_TIMEOUT_S
from amplifier_browser_bridge.update_extension import run_update_extension as _run_update_extension
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

# Appended to the four device-addressing tools' descriptions. Before the
# consolidation this text was repeated on 20 separate tools (235 chars x 20);
# four subject-area tools now carry it once each.
_QUEUE_NOTE = (
    'Non-live device: {"status": "queued", command_id, tier, queue_position} instead of {"ok": ...} -- '
    'normal and actionable, not an error; browser_devices(operation="poll") returns the eventual result.'
)


def _client() -> HubClient:
    return HubClient(DEFAULT_HUB_URL, token=DEFAULT_TOKEN)


Runner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class _Op(NamedTuple):
    """One operation inside a subject-area tool.

    `line` is the single description line the agent sees for this operation
    (<= ~160 chars -- longer contract detail goes in the NOTES block under the
    operation list, or in the parameter's own description). `required` names the
    parameters this operation cannot run without; JSON Schema cannot express
    "required, but only for operation=X", so it is enforced in `execute` and
    reported by name rather than failing somewhere deeper with a KeyError.
    """

    line: str
    required: tuple[str, ...]
    runner: Runner


class _HubTool:
    """Generic thin Amplifier tool: one instance per subject area.

    Holds no command-specific logic of its own -- it exists only to satisfy the
    Tool protocol (name/description/input_schema/execute) around the `_Op`
    registry it's constructed with. Each op's `runner` is a small function that
    maps `input_data` to one `HubClient` call.
    """

    def __init__(
        self, name: str, header: str, ops: dict[str, _Op], props: dict[str, Any], notes: str = ""
    ) -> None:
        self._name = name
        self._ops = ops
        op_lines = "\n".join(f"- {op_name}: {op.line}" for op_name, op in ops.items())
        self._description = "\n".join(part for part in (header, op_lines, notes) if part)
        self._input_schema = {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": list(ops),
                    "description": "Which operation to perform -- see the tool description.",
                },
                **props,
            },
            "required": ["operation"],
        }

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
        """Dispatch on `operation`, then pass the hub's response straight back.

        This is the one place the tier pass-through guarantee lives for this
        surface: whatever dict the hub returned (ok/result, ok/error, or
        status=queued/tier/...) becomes `ToolResult(success=True, output=<that
        dict>)` verbatim. `success=False` is reserved for adapter-level failures
        (an unknown/missing operation, a missing required parameter, or a
        HubError -- e.g. the hub itself is unreachable), not for `ok: false`
        command results, which are legitimate data the calling agent must see.
        """
        operation = input_data.get("operation")
        op = self._ops.get(operation) if isinstance(operation, str) else None
        if op is None:
            return ToolResult(
                success=False,
                output=(
                    f"{self._name}: unknown operation {operation!r}. "
                    f"Valid operations: {', '.join(self._ops)}."
                ),
            )
        missing = [p for p in op.required if input_data.get(p) is None]
        if missing:
            return ToolResult(
                success=False,
                output=f"{self._name}(operation={operation!r}) requires: {', '.join(missing)}.",
            )
        try:
            result = await op.runner(input_data)
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
# Shared parameters, declared ONCE and spread into whichever tools need them --
# the point of the consolidation. Before this, device_id/tab_id/window_id/
# timeout_s were re-serialised into 20+ separate tool schemas.
# ---------------------------------------------------------------------------

_DEVICE_ID_PROP = {"device_id": {"type": "string", "description": "Device id, from browser_devices."}}
_SESSION_ID_PROP = {
    "session_id": {
        "type": "string",
        "description": (
            "Optional write-scope session id from browser_admin(operation='establish_session'); the "
            "hub enforces that scope on click/type/key/navigate before they reach the device."
        ),
    }
}
_TAB_TARGET_PROPS = {
    **_DEVICE_ID_PROP,
    "tab_id": {"type": "integer", "description": "Tab id, from browser_tabs(operation='list')."},
    "window_id": {"type": "integer", "description": "Optional window id (disambiguates reused tab ids)."},
    "timeout_s": {
        "type": "number",
        "description": (
            "Override the hub's default device-round-trip wait, in seconds, for this call only -- "
            "for a heavy/slow-hydrating page (docs/PROTOCOL.md, 'Command timeout')."
        ),
    },
}
_TAB_IDS_PROP = {
    "tab_ids": {
        "type": "array",
        "items": {"type": "integer"},
        "description": "Restrict to this subset of tabs. Omit for every tab.",
    }
}
_MAX_BYTES_PROP = {
    "max_bytes": {"type": "integer", "description": "Raise the 25MB byte-size cap for this fetch."}
}
_CAPTURE_SHAPE_PROPS = {
    "frame_id": {"type": "integer", "description": "Crop to this frame's on-screen region."},
    "multi_page": {"type": "boolean", "default": False},
    "max_pages": {"type": "integer", "default": 10, "description": "Cap for multi_page (max 50)."},
    "scroll_selector": {"type": "string", "description": "CSS selector of the scroll container."},
    "page_delay_ms": {"type": "integer", "description": "Settle delay between scroll and capture."},
}

# ---------------------------------------------------------------------------
# COMPATIBILITY TABLE.
#
# Every one of the 31 flat tools this module mounted before the consolidation,
# mapped to exactly one (tool, operation) pair. Nothing was deleted: the five
# operations nobody has ever called (download start, narrow_scope, archive
# convert, archive catalog, update_extension) are still here, marked RARELY
# USED in their description lines rather than dropped.
#
# tests/test_consolidated_surface.py fails if any former name stops resolving,
# if two map to the same pair, or if any live operation has no former name.
# ---------------------------------------------------------------------------

LEGACY_TOOLS: dict[str, tuple[str, str]] = {
    "browser_devices": ("browser_devices", "list"),
    "browser_poll": ("browser_devices", "poll"),
    "browser_tabs": ("browser_tabs", "list"),
    "browser_tab_open": ("browser_tabs", "open"),
    "browser_tab_close": ("browser_tabs", "close"),
    "browser_tab_activate": ("browser_tabs", "activate"),
    "browser_snapshot": ("browser_page", "snapshot"),
    "browser_read": ("browser_page", "read"),
    "browser_click": ("browser_page", "click"),
    "browser_type": ("browser_page", "type"),
    "browser_key": ("browser_page", "key"),
    "browser_scroll": ("browser_page", "scroll"),
    "browser_navigate": ("browser_page", "navigate"),
    "browser_wait_for": ("browser_page", "wait_for"),
    "browser_wait_text": ("browser_page", "wait_text"),
    "browser_screenshot": ("browser_capture", "screenshot"),
    "browser_vision_read": ("browser_capture", "vision_read"),
    "browser_fetch_bytes": ("browser_capture", "fetch_bytes"),
    "browser_grab_image": ("browser_capture", "grab_image"),
    "browser_downloads_list": ("browser_download", "list"),
    "browser_download": ("browser_download", "start"),
    "browser_wait_download": ("browser_download", "wait"),
    "browser_archive": ("browser_archive", "archive"),
    "browser_archive_convert": ("browser_archive", "convert"),
    "browser_archive_catalog": ("browser_archive", "catalog"),
    "browser_setup": ("browser_admin", "setup"),
    "browser_setup_status": ("browser_admin", "status"),
    "browser_reload": ("browser_admin", "reload"),
    "browser_update_extension": ("browser_admin", "update_extension"),
    "browser_establish_session": ("browser_admin", "establish_session"),
    "browser_narrow_scope": ("browser_admin", "narrow_scope"),
}


def _build_tools() -> list[_HubTool]:
    async def devices_runner(_input_data: dict[str, Any]) -> dict[str, Any]:
        devices = await _client().list_devices()
        return {"ok": True, "devices": devices}

    async def poll_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        return await _client().poll(input_data["device_id"], input_data["command_id"])

    async def tabs_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        # window_id is deliberately NOT forwarded to the Target here -- it is a
        # post-fetch filter applied by shape_tabs_response below, not a wire-level
        # scope. The hub still returns every tab on the device (paging.py's module
        # docstring); this is what lets the shaped response report an honest,
        # unfiltered `total` alongside the filtered `matched` count.
        raw = await _client().command(Target(device_id=input_data["device_id"]), "tabs", {})
        return shape_tabs_response(
            raw,
            window_id=input_data.get("window_id"),
            url_contains=input_data.get("url_contains"),
            title_contains=input_data.get("title_contains"),
            limit=input_data.get("limit", DEFAULT_LIMIT),
            offset=input_data.get("offset", 0),
            summary=bool(input_data.get("summary", False)),
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

    async def setup_status_runner(_input_data: dict[str, Any]) -> dict[str, Any]:
        checks = await run_doctor(DEFAULT_HUB_URL, DEFAULT_TOKEN)
        return {
            "ok": all(c.status != "fail" for c in checks),
            "checks": [
                {"name": c.name, "status": c.status, "message": c.message, "detail": c.detail} for c in checks
            ],
        }

    async def update_extension_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        return await _run_update_extension(
            _client(),
            input_data["device_id"],
            reconnect_timeout_s=input_data.get("reconnect_timeout_s", DEFAULT_RECONNECT_TIMEOUT_S),
        )

    async def archive_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        try:
            return await _run_archive(
                _client(),
                input_data["device_id"],
                input_data["dest_dir"],
                depth=input_data.get("depth", DEFAULT_DEPTH),
                tab_ids=input_data.get("tab_ids"),
                include_cookies=bool(input_data.get("include_cookies", False)),
                wake=bool(input_data.get("wake", False)),
                all_frames=bool(input_data.get("all_frames", False)),
                timeout_s=input_data.get("timeout_s"),
                injection_timeout_s=input_data.get("injection_timeout_s"),
                captures=input_data.get("captures"),
            )
        except ArchiveError as e:
            return {"ok": False, "error": str(e)}

    async def archive_convert_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        try:
            return _run_archive_convert(
                input_data["archive_dir"],
                tab_ids=input_data.get("tab_ids"),
            )
        except ConversionError as e:
            return {"ok": False, "error": str(e)}

    async def archive_catalog_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        try:
            return await _run_archive_catalog(
                input_data["archive_dir"],
                lens=input_data.get("lens"),
                catalog=bool(input_data.get("catalog", False)),
                tab_ids=input_data.get("tab_ids"),
                concurrency=input_data.get("concurrency", DEFAULT_CONCURRENCY),
                top_n=input_data.get("top_n", DEFAULT_TOP_N),
            )
        except CatalogError as e:
            return {"ok": False, "error": str(e)}

    async def setup_runner(input_data: dict[str, Any]) -> dict[str, Any]:
        return await run_auto_setup(
            host=input_data.get("host"),
            port=input_data.get("port", DEFAULT_PORT),
            token_file=input_data.get("token_file"),
            dest=input_data.get("dest"),
            install_service=bool(input_data.get("install_service", True)),
            force_token=bool(input_data.get("force_token", False)),
            wait_reachable_s=float(input_data.get("wait_reachable_s", DEFAULT_WAIT_REACHABLE_S)),
        )

    return [
        _HubTool(
            "browser_devices",
            "Browser devices, and the results of commands they have not run yet. ALWAYS call "
            'operation="list" first -- every other tool\'s device_id comes from nowhere else.',
            {
                "list": _Op(
                    "every known device: id, label, platform, connectivity tier "
                    "(live/intermittent/dormant), behaviourally-probed capabilities (e.g. "
                    "debugger/CDP), queue length.",
                    (),
                    devices_runner,
                ),
                "poll": _Op(
                    'check on / retrieve a queued command: {"status":"queued", queue_position, '
                    'tier}, then {"status":"pending"} while it runs, then the final {"ok":...}.',
                    ("device_id", "command_id"),
                    poll_runner,
                ),
            },
            {
                **_DEVICE_ID_PROP,
                "command_id": {"type": "string", "description": "command_id from a queued result."},
            },
        ),
        _HubTool(
            "browser_tabs",
            "A device's tabs: inventory and lifecycle. Every other tool's tab_id comes from "
            'operation="list".',
            {
                "list": _Op(
                    "paged, filtered tab inventory; each entry carries discarded/status. Reports "
                    "total (unfiltered), matched, returned, offset, limit, has_more.",
                    ("device_id",),
                    tabs_runner,
                ),
                "open": _Op(
                    "new tab, device-only target. active defaults to false, so it opens in the "
                    "background without stealing focus.",
                    ("device_id",),
                    tab_open_runner,
                ),
                "close": _Op(
                    "close one tab.",
                    ("device_id", "tab_id"),
                    lambda input_data: _command("tab_close", _no_args, input_data),
                ),
                "activate": _Op(
                    "foreground a tab -- the one operation allowed to steal focus, because it was "
                    "asked to. Prefer background tabs wherever a command allows it.",
                    ("device_id", "tab_id"),
                    lambda input_data: _command("tab_activate", _no_args, input_data),
                ),
            },
            {
                **_TAB_TARGET_PROPS,
                "url": {
                    "type": "string",
                    "default": "about:blank",
                    "description": "open: url for the new tab.",
                },
                "active": {
                    "type": "boolean",
                    "default": False,
                    "description": "open: foreground the new tab.",
                },
                "url_contains": {
                    "type": "string",
                    "description": "list: filter by url substring (case-insensitive).",
                },
                "title_contains": {
                    "type": "string",
                    "description": "list: filter by title substring (case-insensitive).",
                },
                "limit": {
                    "type": "integer",
                    "default": DEFAULT_LIMIT,
                    "description": "list: 0 means unlimited.",
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "list: skip this many matched tabs.",
                },
                "summary": {
                    "type": "boolean",
                    "default": False,
                    "description": "list: counts only, no tab list.",
                },
            },
            "list is PAGED by default (limit=100, offset=0; limit=0 unpaged), and "
            "window_id/url_contains/title_contains filter BEFORE offset/limit. Against an "
            "unknown-size profile call summary=true FIRST -- per-window counts, totals and "
            "discarded/asleep only, no tab list at all -- because an unpaged listing of hundreds "
            "of tabs can truncate before it reaches your context.\n" + _QUEUE_NOTE,
        ),
        _HubTool(
            "browser_page",
            'Read and act on one tab. Element refs come from operation="snapshot": each node '
            "carries a generation, a ref is valid only from the MOST RECENT snapshot of that "
            "frame, and a superseded one fails loud with 'stale ref' rather than silently doing "
            "nothing. Refs reset on navigation.",
            {
                "snapshot": _Op(
                    "accessibility-style element tree with frame-qualified ref ids (e.g. 'f0.e12') "
                    "for click/type/key, plus each frame's on-screen region in `frames`.",
                    ("device_id", "tab_id"),
                    lambda input_data: _command("snapshot", read_or_snapshot_args, input_data),
                ),
                "read": _Op(
                    "full visible text. Use when you want content, not refs; canvas-rendered "
                    "content is never in the DOM -- see browser_capture vision_read.",
                    ("device_id", "tab_id"),
                    lambda input_data: _command("read", read_or_snapshot_args, input_data),
                ),
                "click": _Op(
                    "click the element at ref.",
                    ("device_id", "tab_id", "ref"),
                    lambda input_data: _command("click", click_args, input_data),
                ),
                "type": _Op(
                    "type `text` into the element at ref.",
                    ("device_id", "tab_id", "ref", "text"),
                    lambda input_data: _command("type", type_args, input_data),
                ),
                "key": _Op(
                    "press `key` (e.g. 'Enter', 'Escape', 'Tab'), optionally focusing ref first.",
                    ("device_id", "tab_id", "key"),
                    lambda input_data: _command("key", key_args, input_data),
                ),
                "scroll": _Op(
                    "scroll to absolute x, y.",
                    ("device_id", "tab_id"),
                    lambda input_data: _command("scroll", scroll_args, input_data),
                ),
                "navigate": _Op(
                    "go to url.",
                    ("device_id", "tab_id", "url"),
                    lambda input_data: _command("navigate", navigate_args, input_data),
                ),
                "wait_for": _Op(
                    "poll (never sleep blindly) until CSS `selector` matches, or timeout_ms elapses.",
                    ("device_id", "tab_id", "selector"),
                    lambda input_data: _command("wait_for", wait_for_args, input_data),
                ),
                "wait_text": _Op(
                    "poll (never sleep blindly) until the visible text contains `text`, or "
                    "timeout_ms elapses.",
                    ("device_id", "tab_id", "text"),
                    lambda input_data: _command("wait_text", wait_text_args, input_data),
                ),
            },
            {
                **_TAB_TARGET_PROPS,
                **_SESSION_ID_PROP,
                "wake": {"type": "boolean", "default": False},
                "activate": {"type": "boolean", "default": False},
                "ref": {"type": "string", "description": "Element ref from a prior snapshot."},
                "text": {
                    "type": "string",
                    "description": "type: text to type. wait_text: substring to wait for.",
                },
                "key": {"type": "string", "description": "key: key name, e.g. 'Enter'."},
                "x": {"type": "integer", "default": 0},
                "y": {"type": "integer", "default": 0},
                "url": {"type": "string", "description": "navigate: url to load."},
                "selector": {"type": "string", "description": "wait_for: CSS selector to wait for."},
                "timeout_ms": {
                    "type": "integer",
                    "default": 10000,
                    "description": "wait_for/wait_text deadline.",
                },
            },
            "snapshot/read on a discarded background tab (browser_tabs' `discarded`) fail loud "
            "naming that cause. wake=true reloads and retries, DESTROYING unsaved in-page state, "
            "and reports 'woke': true; activate=true foregrounds a heavy/slow-hydrating SPA "
            "first, stealing focus, and reports 'activated': true. Neither is ever automatic.\n"
            + _QUEUE_NOTE,
        ),
        _HubTool(
            "browser_capture",
            "Get pixels or bytes out of a page.",
            {
                "screenshot": _Op(
                    "PIXELS (base64 + format), no model call. Use when you can see images directly.",
                    ("device_id", "tab_id"),
                    lambda input_data: _command("screenshot", screenshot_args, input_data),
                ),
                "vision_read": _Op(
                    "pixels -> TEXT via a real, separate vision-model call. Use only when the "
                    "content was never in the DOM (a canvas-rendered viewer, e.g. Word Online).",
                    ("device_id", "tab_id"),
                    vision_read_runner,
                ),
                "fetch_bytes": _Op(
                    "fetch `url` from the EXTENSION's own cookied context; no tab_id needed. For a "
                    "file a page only links to (.docx/.pdf) behind the user's login.",
                    ("device_id", "url"),
                    fetch_bytes_runner,
                ),
                "grab_image": _Op(
                    "fetch `url` from the PAGE's own script context, carrying its Referer -- the "
                    "fallback when fetch_bytes trips hotlink/Referer protection.",
                    ("device_id", "tab_id", "url"),
                    lambda input_data: _command("grab_image", grab_image_args, input_data),
                ),
            },
            {
                **_TAB_TARGET_PROPS,
                "capture_hidden": {
                    "type": "boolean",
                    "description": (
                        "Capture a tab that is NOT the active tab of a focused window. Defaults "
                        "false for screenshot, true for vision_read."
                    ),
                },
                **_CAPTURE_SHAPE_PROPS,
                "prompt": {
                    "type": "string",
                    "description": "vision_read: what to extract from the image(s).",
                },
                "url": {"type": "string", "description": "fetch_bytes/grab_image: url to fetch."},
                **_MAX_BYTES_PROP,
            },
            "capture_hidden auto-escalates to CDP and needs browser_devices' "
            "capabilities.debugger; without it the capture fails loud rather than silently "
            "activating the tab, and on Android (no CDP) only the active tab is ever capturable. "
            "frame_id crops to one frame's region and requires capture_hidden. multi_page=true "
            "scrolls and re-captures up to max_pages (default 10, hard cap 50) until the "
            "scrollable region ends, returning a `pages` array plus capped/stopped_reason -- "
            "never a partial result reported as complete.\n"
            "vision_read needs a provider env var on the machine running this hub "
            "(ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY, or "
            "AMPLIFIER_BROWSER_BRIDGE_VISION_PROVIDER to pin one); with none set it fails loud "
            "with setup instructions and never returns empty text. It returns text, "
            "vision_provider, vision_model, image_count, page_count, capped, stopped_reason.\n"
            "fetch_bytes/grab_image return {url, content_type, byte_length, base64} and refuse "
            "past a 25MB cap unless max_bytes raises it.\n" + _QUEUE_NOTE,
        ),
        _HubTool(
            "browser_download",
            "Downloads on a device.",
            {
                "list": _Op(
                    "recent downloads (chrome.downloads.search) plus max_download_id, the highest "
                    "download id chrome currently knows about.",
                    ("device_id",),
                    downloads_list_runner,
                ),
                "start": _Op(
                    "RARELY USED. Trigger a download of `url` directly (chrome.downloads.download); "
                    "returns a download_id this call definitely owns.",
                    ("device_id", "url"),
                    download_runner,
                ),
                "wait": _Op(
                    "poll for a completed download: {download_id, filename, url, mime, "
                    "byte_length, state}, or an error if interrupted or timeout_ms passed.",
                    ("device_id",),
                    wait_download_runner,
                ),
            },
            {
                **_DEVICE_ID_PROP,
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "list: how many recent downloads.",
                },
                "url": {"type": "string", "description": "start: url to download."},
                "filename": {"type": "string", "description": "start: suggested filename."},
                "download_id": {"type": "integer", "description": "wait: the id start returned."},
                "since_id": {"type": "integer", "description": "wait: baseline max_download_id from list."},
                "pattern": {"type": "string", "description": "wait: optional regex on the filename."},
                "timeout_ms": {"type": "integer", "default": 30000, "description": "wait: deadline."},
            },
            "Pass EXACTLY ONE of download_id or since_id to wait. Take since_id from list BEFORE "
            "the action that triggers an indirect download (clicking a page's own Download "
            "control): since_id never matches a download at or below that baseline, so it "
            "structurally cannot claim one the human started. pattern narrows a since_id search "
            "by filename.\n" + _QUEUE_NOTE,
        ),
        _HubTool(
            "browser_archive",
            "Capture a browser's whole state to disk, then optionally process it locally. Every "
            "operation returns a MANIFEST -- paths, counts, byte sizes, per-tab status -- never "
            "the payload itself.",
            {
                "archive": _Op(
                    "capture a device at depth L0 (inventory only, no page contact) through L5 "
                    "(+MHTML, navigation history, profile data) into dest_dir.",
                    ("device_id", "dest_dir"),
                    archive_runner,
                ),
                "convert": _Op(
                    "RARELY USED. Turn an existing archive's captured MHTML into markdown. Local "
                    "CPU only, no browser interaction.",
                    ("archive_dir",),
                    archive_convert_runner,
                ),
                "catalog": _Op(
                    "RARELY USED. Inventory an existing archive's tabs; catalog=true adds an "
                    "opt-in per-tab LLM judgment.",
                    ("archive_dir",),
                    archive_catalog_runner,
                ),
            },
            {
                **_DEVICE_ID_PROP,
                "dest_dir": {
                    "type": "string",
                    "description": "archive: base dir for a fresh timestamped archive.",
                },
                "depth": {
                    "type": "string",
                    "enum": ["L0", "L1", "L2", "L3", "L4", "L5"],
                    "default": DEFAULT_DEPTH,
                    "description": "archive: see the depth ladder in the tool description.",
                },
                **_TAB_IDS_PROP,
                "include_cookies": {
                    "type": "boolean",
                    "default": False,
                    "description": "archive: opt in to cookies at L5. Never implied by depth alone.",
                },
                "wake": {
                    "type": "boolean",
                    "default": False,
                    "description": "archive: allow waking a slept tab.",
                },
                "all_frames": {
                    "type": "boolean",
                    "default": False,
                    "description": "archive: forwarded to the L1 text capture only.",
                },
                "timeout_s": {
                    "type": "number",
                    "description": "Per-command device-round-trip timeout override.",
                },
                "injection_timeout_s": {
                    "type": "number",
                    "description": (
                        "archive: overrides timeout_s for the JS-injection captures (text/L1, "
                        "dom/L2) only; CDP captures keep timeout_s."
                    ),
                },
                "captures": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["text", "dom", "screenshot", "mhtml", "nav_history"],
                    },
                    "description": (
                        "archive: allow-list that NARROWS -- never widens -- which per-tab "
                        "captures run at this depth. An excluded one is recorded skipped."
                    ),
                },
                "archive_dir": {
                    "type": "string",
                    "description": "convert/catalog: the archive dir a prior archive manifest reported.",
                },
                "catalog": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "catalog: opt in to Layer 2 (per-tab LLM judgment). False returns only "
                        "the free local Layer 1 inventory."
                    ),
                },
                "lens": {
                    "type": "string",
                    "description": "catalog: freeform reader context, used only when catalog=true.",
                },
                "concurrency": {
                    "type": "integer",
                    "default": DEFAULT_CONCURRENCY,
                    "description": "catalog: concurrent per-tab model calls.",
                },
                "top_n": {
                    "type": "integer",
                    "default": DEFAULT_TOP_N,
                    "description": "catalog: entries in Layer 1's duplicates/by_domain lists.",
                },
            },
            "Depth ladder, each level a strict superset of the one below: L0 inventory (no tab "
            "wake, no page contact); L1 +visible text; L2 +DOM/forms/localStorage/sessionStorage/"
            "scroll; L3 +screenshots; L4 +MHTML; L5 +per-tab navigation history and browser-wide "
            "profile data. L4/L5 need browser_devices' capabilities.debugger and fail loud "
            "immediately, before anything is captured, without it.\n"
            "NO-WAKE GUARANTEE: waking a discarded/asleep tab destroys unsaved in-page state, so "
            "every such tab is SKIPPED for L1+ capture -- recorded in the manifest, never "
            "silently dropped -- unless wake=true.\n"
            "manifest['status'] is 'ok' only if nothing failed or was skipped, else "
            "'ok_with_skips'/'ok_with_failures', and manifest['failures'] lists every one. "
            "Per-tab status is five-valued, not binary: ok, failed, partial, skipped, not_found. "
            "Counts of what EXISTS (tabs_inventoried/windows_inventoried/tab_groups_inventoried) "
            "are never collapsed into counts of what was CAPTURED (tabs_captured/tabs_skipped/"
            "tabs_failed/tabs_partial/tabs_not_found), which are honestly 0 at L0 -- success, not "
            "an empty archive.\n"
            "convert's two output files and catalog's two layers: docs/AGENT_SURFACES.md.",
        ),
        _HubTool(
            "browser_admin",
            "Setup, diagnosis and write-scope sessions -- no browser page involved.",
            {
                "setup": _Op(
                    "get from 'bundle installed' to 'browser connected': token, staged extension, "
                    "hub host, background service, and ONE pairing link (result.pairing.pair_url).",
                    (),
                    setup_runner,
                ),
                "status": _Op(
                    "diagnose which link in the setup chain is broken: token store, hub location, "
                    "network exposure, service, reachability, token match, connected browsers.",
                    (),
                    setup_status_runner,
                ),
                "reload": _Op(
                    "reload the extension on a device (chrome.runtime.reload()) so edited unpacked "
                    "files take effect.",
                    ("device_id",),
                    reload_runner,
                ),
                "update_extension": _Op(
                    "RARELY USED. Restage + reload a device's extension, then VERIFY by "
                    "re-reading its command set; else a `guided` block with a download_url.",
                    ("device_id",),
                    update_extension_runner,
                ),
                "establish_session": _Op(
                    "RARELY USED. New session with a caller-declared write scope the page can "
                    "never touch; pass its session_id to browser_page writes.",
                    (),
                    establish_session_runner,
                ),
                "narrow_scope": _Op(
                    "RARELY USED. Narrow an existing session -- never widens; SEALED once the "
                    "session has ingested any page content.",
                    ("session_id",),
                    narrow_scope_runner,
                ),
            },
            {
                **_DEVICE_ID_PROP,
                "host": {
                    "type": "string",
                    "description": (
                        "setup: explicit hub bind/advertise host. Default: this machine's "
                        "Tailscale IP, falling back to 127.0.0.1 (loopback-only)."
                    ),
                },
                "port": {"type": "integer", "default": DEFAULT_PORT, "description": "setup: hub port."},
                "install_service": {
                    "type": "boolean",
                    "default": True,
                    "description": "setup: install/start the hub as a background OS service.",
                },
                "force_token": {
                    "type": "boolean",
                    "default": False,
                    "description": "setup: rotate the hub token (requires re-pasting it into every browser).",
                },
                "wait_reachable_s": {
                    "type": "number",
                    "default": DEFAULT_WAIT_REACHABLE_S,
                    "description": "setup: how long to wait for the hub to answer.",
                },
                "token_file": {
                    "type": "string",
                    "description": "setup: advanced, override the token file path.",
                },
                "dest": {
                    "type": "string",
                    "description": "setup: advanced, override the staged-extension dir.",
                },
                "reconnect_timeout_s": {
                    "type": "number",
                    "default": DEFAULT_RECONNECT_TIMEOUT_S,
                    "description": "update_extension: how long to wait for the device to reconnect.",
                },
                "session_id": {
                    "type": "string",
                    "description": "narrow_scope: the session to narrow.",
                },
                "write": {
                    "type": "string",
                    "description": "'*' (unrestricted) or comma-separated hostnames, subdomain-inclusive.",
                },
                "read": {"type": "string", "description": "As write, for reads."},
                "on_unknown": {"type": "string", "enum": ["allow", "gate", "deny"], "default": "allow"},
                "redeem": {"type": "string", "enum": ["agent", "unredeemable"], "default": "agent"},
                "unattended": {"type": "boolean", "default": False},
            },
            "setup is safe to re-run: an existing token is reused, never rotated, unless "
            "force_token=true, and an already-configured browser's saved settings are never "
            "touched. If the hub is not reachable yet, result.hub_reachable is false, "
            "result.pairing is null, and result.warnings/result.service/result.manual_hub_command "
            "name exactly what is missing (result.setup_url is the manual route). status runs the "
            "same checks as `amplifier-browser-bridge doctor`.\n"
            "narrow_scope may only shrink write/read, move on_unknown allow -> gate -> deny, "
            "redeem agent -> unredeemable, and unattended false -> true.",
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
