"""MCP server -- thin adapter exposing amplifier_browser_bridge as MCP tools.

Satisfies design doc section 3.3's "or maybe just any agent" row: any MCP-speaking
client (Claude Desktop, an Amplifier bundle, a bare `mcp` CLI session, ...) can drive
the user's real, logged-in Edge browsers through this server, with zero Amplifier
dependency.

Every tool here is a THIN wrapper over `HubClient` (see client.py). No policy, no
business logic lives in this file -- it exists to translate MCP's tool-call shape
into a `HubClient.command()`/`list_devices()`/`poll()` call and hand the hub's
response straight back. The one thing this adapter must never do is flatten,
re-shape, or swallow a `{"status": "queued", ...}` response -- see `_run_command`.

Tool naming deliberately mirrors Playwright MCP (`snapshot`/`click`/`type`/`navigate`/
`tabs`/`wait_*`, design doc section 3.3 and section 9) with a `browser_` prefix, so
models that have already seen Playwright MCP recognize most of this vocabulary.

Run it:

    abb-mcp                       # stdio transport (the default every MCP client speaks)
    ABB_MCP_TRANSPORT=sse abb-mcp # or streamable-http, if a client needs it

Configure the hub connection via the same env vars the CLI uses:

    ABB_HUB_URL   -- e.g. ws://100.124.126.19:8900/agent (default ws://127.0.0.1:8900/agent)
    ABB_TOKEN     -- per-device/agent shared token, if the hub has auth enabled
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .addressing import Target
from .client import HubClient, HubError

DEFAULT_HUB_URL = os.environ.get("ABB_HUB_URL", "ws://127.0.0.1:8900/agent")
DEFAULT_TOKEN = os.environ.get("ABB_TOKEN")

mcp = FastMCP(
    name="amplifier-browser-bridge",
    instructions=(
        "Drive the user's real, logged-in Microsoft Edge browser on OTHER devices "
        "(desktop or Android), over the user's own Tailscale network. You are a "
        "second operator sharing a live browsing session with the user -- not a "
        "robot driving a disposable browser: never steal focus, never open tabs "
        "unasked, prefer acting on background tabs.\n\n"
        "START HERE: call browser_devices() first. It lists every known device with "
        "its device_id, connectivity tier, and capabilities -- this is how you "
        "discover what device_id values are valid to address. Then call "
        "browser_tabs(device_id) to see open tabs and get tab_id values. Every other "
        "tool takes device_id (and usually tab_id) to address a specific target -- "
        "there is no implicit 'current' device or tab.\n\n"
        "TIER MODEL: a device is 'live' (connected now, commands execute "
        "immediately), 'intermittent' (mobile, recently seen, self-heals in "
        "roughly 1-2 minutes), or 'dormant' (disconnected a while, or never seen; "
        "commands queue until it wakes). A command sent to a non-live device "
        "returns immediately with a queued status -- it never blocks. See each "
        "tool's description for the exact shape, and use browser_poll to check "
        "back later."
    ),
)


def _client() -> HubClient:
    return HubClient(DEFAULT_HUB_URL, token=DEFAULT_TOKEN)


async def _run_command(
    device_id: str,
    command: str,
    args: dict[str, Any],
    *,
    window_id: int | None = None,
    tab_id: int | None = None,
) -> dict[str, Any]:
    """Shared adapter: send one command, hand the hub's response straight back.

    This is the single place the tier pass-through guarantee is implemented: the
    dict returned here is exactly whatever the hub sent -- `{"ok": ..., "result"/
    "error": ...}` for a live device, or `{"status": "queued", "tier": ..., ...}`
    for a non-live one. Never flattened, never re-shaped, never swallowed.
    """
    target = Target(device_id=device_id, window_id=window_id, tab_id=tab_id)
    try:
        return await _client().command(target, command, args)
    except HubError as e:
        return {"ok": False, "error": str(e)}


# This exact sentence is repeated verbatim in every tab-acting tool's docstring
# below. It is NOT spliced on at runtime: a docstring is only recognized as such
# when it's a literal string as the first statement in a function body, so an
# `fn.__doc__ = ...` or `"""..."""  + SOME_CONSTANT` trick would silently leave
# `__doc__` as `None` (an unused expression) and FastMCP would register the tool
# with no description at all. Plain repeated text is the simple, correct choice.
#
# QUEUE_NOTE (for reference, not used programmatically -- see each docstring):
# "If the device is not 'live', this returns immediately as {"status": "queued",
# "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...} instead
# of {"ok": ...}. That is a normal, actionable result, not an error or a hang --
# call browser_poll(device_id, command_id) later to retrieve the eventual result."


@mcp.tool()
async def browser_devices() -> dict[str, Any]:
    """List every known browser device (every device the hub has ever received a
    `hello` from). ALWAYS call this first -- it is the entry point for addressing
    every other tool.

    Each entry includes: device_id, profile_id, label (e.g. 'edge-macos',
    'edge-android'), platform, protocol_version, connected, last_seen, queue_length,
    and:

    - tier: 'live' (commands execute now), 'intermittent' (mobile, recently seen,
      commands queue but typically drain within roughly 1-2 minutes), or 'dormant'
      (disconnected a while or never seen; commands queue until it reconnects).
    - capabilities: behaviorally-probed booleans -- scripting, windows, tab_groups,
      storage, alarms, downloads, debugger (Chrome DevTools Protocol; always False
      in this phase, and genuinely absent on Android), capture_visible_tab
      (screenshot support -- on Android this only ever captures the CURRENTLY
      ACTIVE tab; capturing a background/non-active tab is desktop-only).
    """
    try:
        devices = await _client().list_devices()
    except HubError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "devices": devices}


@mcp.tool()
async def browser_tabs(device_id: str, window_id: int | None = None) -> dict[str, Any]:
    """List open tabs on a device, optionally scoped to one window_id. Use this
    after browser_devices() to discover tab_id values for the other tools.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(device_id, "tabs", {}, window_id=window_id)


@mcp.tool()
async def browser_snapshot(device_id: str, tab_id: int, window_id: int | None = None) -> dict[str, Any]:
    """Accessibility-style snapshot of a tab: a tree of elements with stable `ref`
    ids (e.g. 'e12') you can pass to browser_click/browser_type/browser_key. Refs
    reset on navigation -- take a fresh snapshot after navigating.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(device_id, "snapshot", {}, window_id=window_id, tab_id=tab_id)


@mcp.tool()
async def browser_read(device_id: str, tab_id: int, window_id: int | None = None) -> dict[str, Any]:
    """Read the full visible text of a tab.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(device_id, "read", {}, window_id=window_id, tab_id=tab_id)


@mcp.tool()
async def browser_click(
    device_id: str, tab_id: int, ref: str, window_id: int | None = None
) -> dict[str, Any]:
    """Click an element by ref (from a prior browser_snapshot call).

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(device_id, "click", {"ref": ref}, window_id=window_id, tab_id=tab_id)


@mcp.tool()
async def browser_type(
    device_id: str, tab_id: int, ref: str, text: str, window_id: int | None = None
) -> dict[str, Any]:
    """Type text into an element by ref (from a prior browser_snapshot call).

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(
        device_id, "type", {"ref": ref, "text": text}, window_id=window_id, tab_id=tab_id
    )


@mcp.tool()
async def browser_key(
    device_id: str, tab_id: int, key: str, ref: str | None = None, window_id: int | None = None
) -> dict[str, Any]:
    """Send a key press (e.g. 'Enter', 'Escape', 'Tab'), optionally focused on a
    specific element ref first.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    args: dict[str, Any] = {"key": key}
    if ref is not None:
        args["ref"] = ref
    return await _run_command(device_id, "key", args, window_id=window_id, tab_id=tab_id)


@mcp.tool()
async def browser_scroll(
    device_id: str, tab_id: int, x: int = 0, y: int = 0, window_id: int | None = None
) -> dict[str, Any]:
    """Scroll a tab to absolute coordinates (x, y).

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(device_id, "scroll", {"x": x, "y": y}, window_id=window_id, tab_id=tab_id)


@mcp.tool()
async def browser_navigate(
    device_id: str, tab_id: int, url: str, window_id: int | None = None
) -> dict[str, Any]:
    """Navigate a tab to a URL.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(device_id, "navigate", {"url": url}, window_id=window_id, tab_id=tab_id)


@mcp.tool()
async def browser_tab_open(device_id: str, url: str = "about:blank", active: bool = False) -> dict[str, Any]:
    """Open a new tab on a device. No tab_id exists yet, so target is device-only.
    `active` defaults to False (co-working etiquette: don't steal focus) -- the new
    tab opens in the background unless you explicitly ask for active=True.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(device_id, "tab_open", {"url": url, "active": active})


@mcp.tool()
async def browser_tab_close(device_id: str, tab_id: int, window_id: int | None = None) -> dict[str, Any]:
    """Close a tab.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(device_id, "tab_close", {}, window_id=window_id, tab_id=tab_id)


@mcp.tool()
async def browser_tab_activate(device_id: str, tab_id: int, window_id: int | None = None) -> dict[str, Any]:
    """Bring a tab to the foreground. This is the one command explicitly allowed to
    steal focus, because it was asked to -- prefer acting on background tabs
    wherever a command allows it (co-working etiquette, design doc section 6.3).

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(device_id, "tab_activate", {}, window_id=window_id, tab_id=tab_id)


@mcp.tool()
async def browser_screenshot(device_id: str, tab_id: int, window_id: int | None = None) -> dict[str, Any]:
    """Screenshot a tab. In this injection-only phase (no CDP yet), this only
    succeeds if the target tab is already the active tab of a focused window -- it
    fails loud rather than silently activating the tab to comply. Check
    browser_devices()'s capabilities.capture_visible_tab first; on Android this is
    the ONLY way to see a tab, and only ever the currently-active one (design doc
    section 7).

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(device_id, "screenshot", {}, window_id=window_id, tab_id=tab_id)


@mcp.tool()
async def browser_wait_for(
    device_id: str, tab_id: int, selector: str, timeout_ms: int = 10000, window_id: int | None = None
) -> dict[str, Any]:
    """Poll (never sleep blindly) until a CSS selector matches an element, or time
    out after timeout_ms.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(
        device_id,
        "wait_for",
        {"selector": selector, "timeout_ms": timeout_ms},
        window_id=window_id,
        tab_id=tab_id,
    )


@mcp.tool()
async def browser_wait_text(
    device_id: str, tab_id: int, text: str, timeout_ms: int = 10000, window_id: int | None = None
) -> dict[str, Any]:
    """Poll (never sleep blindly) until the tab's visible text contains a
    substring, or time out after timeout_ms.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(
        device_id,
        "wait_text",
        {"text": text, "timeout_ms": timeout_ms},
        window_id=window_id,
        tab_id=tab_id,
    )


@mcp.tool()
async def browser_poll(device_id: str, command_id: str) -> dict[str, Any]:
    """Check on (or retrieve the eventual result of) a command that was previously
    reported as queued. Returns one of three shapes: {"status": "queued",
    "queue_position": ..., "tier": ...} if still waiting for the device, {"status":
    "pending"} if the device is live and executing it right now, or the final
    {"ok": ...} result once it has actually run."""
    try:
        return await _client().poll(device_id, command_id)
    except HubError as e:
        return {"ok": False, "error": str(e)}


def main() -> None:
    """Console-script entry point (`abb-mcp`). Runs over stdio by default -- the
    transport every MCP client (Claude Desktop, Amplifier, `mcp` CLI, ...) speaks
    without extra configuration. Set ABB_MCP_TRANSPORT=sse or streamable-http to
    use a different transport."""
    transport = os.environ.get("ABB_MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
