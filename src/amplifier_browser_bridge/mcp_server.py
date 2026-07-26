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

import base64
import os
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from .addressing import Target
from .client import HubClient, HubError
from .vision import VisionConfigError, VisionError
from .vision_read import vision_read as _vision_read

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
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Shared adapter: send one command, hand the hub's response straight back.

    This is the single place the tier pass-through guarantee is implemented: the
    dict returned here is exactly whatever the hub sent -- `{"ok": ..., "result"/
    "error": ...}` for a live device, or `{"status": "queued", "tier": ..., ...}`
    for a non-live one. Never flattened, never re-shaped, never swallowed.

    `timeout_s`, if given, overrides the hub's default device-round-trip wait for
    THIS command only (see hub.py's DEFAULT_COMMAND_TIMEOUT / HUB_ONLY_ARGS in
    protocol.py) -- useful for a heavy SPA that needs longer than the hub default.
    """
    target = Target(device_id=device_id, window_id=window_id, tab_id=tab_id)
    if timeout_s is not None:
        args = {**args, "timeout_s": timeout_s}
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
async def browser_tabs(
    device_id: str, window_id: int | None = None, timeout_s: float | None = None
) -> dict[str, Any]:
    """List open tabs on a device, optionally scoped to one window_id. Use this
    after browser_devices() to discover tab_id values for the other tools.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(device_id, "tabs", {}, window_id=window_id, timeout_s=timeout_s)


@mcp.tool()
async def browser_snapshot(
    device_id: str,
    tab_id: int,
    window_id: int | None = None,
    wake: bool = False,
    activate: bool = False,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Accessibility-style snapshot of a tab: a tree of elements with stable,
    frame-qualified `ref` ids (e.g. 'f0.e12' -- the 'f<frameId>' prefix identifies
    which frame the element lives in; see the "frames"/"frame_count" fields in the
    result) you can pass to browser_click/browser_type/browser_key. Gathers nodes
    from every frame on the page (iframes included), not just the top frame. Refs
    reset on navigation -- take a fresh snapshot after navigating.

    Each node (and each `frames` entry) also carries a `generation` number: refs
    are only valid from the MOST RECENT snapshot of a given frame. If you take a
    second snapshot and then try to use a ref from the first one, browser_click/
    browser_type/browser_key will fail loud with a specific "stale ref" error
    rather than silently doing nothing -- take a fresh snapshot and use a ref from
    that result instead of reusing an older one.

    At real-world scale (hundreds of tabs) Edge discards (unloads) most background
    tabs to reclaim memory. A discarded tab fails loud with a specific error naming
    the real cause -- check browser_tabs()'s `discarded` field first. Pass wake=True
    to reload a discarded tab and retry; this destroys in-page state (unsaved form
    data, scroll position, ephemeral JS state), so it is opt-in only -- the result
    reports `"woke": true` when this happened.

    A heavy/hydrated SPA can be slow or outright time out while the tab is NOT the
    active tab (DOM injection/traversal is measured to be dramatically faster once
    foregrounded). Pass activate=True to foreground the tab first -- never automatic,
    since it steals the human's focus; the result reports `"activated": true` when
    this happened. If you'd rather not steal focus at all, browser_vision_read
    captures a screenshot and extracts text via a vision model instead (no focus
    steal, costs a model call, produces no element refs).

    `timeout_s`, if given, overrides the hub's default device-round-trip wait for
    just this call -- useful for a heavy/slow-hydrating page (see browser_devices()'s
    module docs and docs/PROTOCOL.md's "Command timeout" section).

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    args: dict[str, Any] = {}
    if wake:
        args["wake"] = True
    if activate:
        args["activate"] = True
    return await _run_command(
        device_id, "snapshot", args, window_id=window_id, tab_id=tab_id, timeout_s=timeout_s
    )


@mcp.tool()
async def browser_read(
    device_id: str,
    tab_id: int,
    window_id: int | None = None,
    wake: bool = False,
    activate: bool = False,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Read the visible text of a tab, gathered across ALL frames (iframes
    included), not just the top frame -- the result's `text`/`url`/`title` are
    the richest (most text) frame found; `frame_count`, `other_frames` (a
    manifest of every other frame's url/title/char-count), and
    `unconfirmed_frames` (child frames declared in the DOM that produced no
    result -- sandboxed, cross-origin-blocked, or not yet loaded) let you see
    what else is on the page. See docs/PROTOCOL.md's "Frames" section.

    At real-world scale (hundreds of tabs) Edge discards (unloads) most background
    tabs to reclaim memory. A discarded tab fails loud with a specific error naming
    the real cause -- check browser_tabs()'s `discarded` field first. Pass wake=True
    to reload a discarded tab and retry; this destroys in-page state (unsaved form
    data, scroll position, ephemeral JS state), so it is opt-in only -- the result
    reports `"woke": true` when this happened.

    A heavy/hydrated SPA can be slow or outright time out while the tab is NOT the
    active tab (DOM injection/traversal is measured to be dramatically faster once
    foregrounded). Pass activate=True to foreground the tab first -- never automatic,
    since it steals the human's focus; the result reports `"activated": true` when
    this happened. If you'd rather not steal focus at all, browser_vision_read
    captures a screenshot and extracts text via a vision model instead (no focus
    steal, costs a model call, produces no element refs).

    `timeout_s`, if given, overrides the hub's default device-round-trip wait for
    just this call -- useful for a heavy/slow-hydrating page (see docs/PROTOCOL.md's
    "Command timeout" section).

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    args: dict[str, Any] = {}
    if wake:
        args["wake"] = True
    if activate:
        args["activate"] = True
    return await _run_command(
        device_id, "read", args, window_id=window_id, tab_id=tab_id, timeout_s=timeout_s
    )


@mcp.tool()
async def browser_click(
    device_id: str, tab_id: int, ref: str, window_id: int | None = None, timeout_s: float | None = None
) -> dict[str, Any]:
    """Click an element by ref (from a prior browser_snapshot call). A
    frame-qualified ref (e.g. 'f3.e7') routes the click to that exact frame.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(
        device_id, "click", {"ref": ref}, window_id=window_id, tab_id=tab_id, timeout_s=timeout_s
    )


@mcp.tool()
async def browser_type(
    device_id: str,
    tab_id: int,
    ref: str,
    text: str,
    window_id: int | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Type text into an element by ref (from a prior browser_snapshot call). A
    frame-qualified ref (e.g. 'f3.e7') routes the input to that exact frame.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(
        device_id,
        "type",
        {"ref": ref, "text": text},
        window_id=window_id,
        tab_id=tab_id,
        timeout_s=timeout_s,
    )


@mcp.tool()
async def browser_key(
    device_id: str,
    tab_id: int,
    key: str,
    ref: str | None = None,
    window_id: int | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Send a key press (e.g. 'Enter', 'Escape', 'Tab'), optionally focused on a
    specific element ref first. A frame-qualified ref (e.g. 'f3.e7') routes the
    keypress to that exact frame; with no ref, the key goes to the top frame.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    args: dict[str, Any] = {"key": key}
    if ref is not None:
        args["ref"] = ref
    return await _run_command(device_id, "key", args, window_id=window_id, tab_id=tab_id, timeout_s=timeout_s)


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
    device_id: str, tab_id: int, url: str, window_id: int | None = None, timeout_s: float | None = None
) -> dict[str, Any]:
    """Navigate a tab to a URL.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(
        device_id, "navigate", {"url": url}, window_id=window_id, tab_id=tab_id, timeout_s=timeout_s
    )


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
async def browser_screenshot(
    device_id: str,
    tab_id: int,
    window_id: int | None = None,
    capture_hidden: bool = False,
    frame_id: int | None = None,
    multi_page: bool = False,
    max_pages: int | None = None,
    scroll_selector: str | None = None,
    page_delay_ms: int | None = None,
    timeout_s: float | None = None,
) -> Any:
    """Screenshot a tab -- returns PIXELS as an MCP image content block (plus a small
    metadata block), no model call. This is the "return pixels" mechanism: a
    vision-capable MCP client can look at the returned image itself with zero extra
    work on this server's part. If you need TEXT extracted from the image instead
    (e.g. your client is text-only, or you want OCR'd text in context rather than an
    image), use browser_vision_read instead -- a distinct, explicitly different
    mechanism that makes a real vision-model API call; this tool never does that.

    capture_hidden=True captures a tab that is NOT the active tab of a focused window
    (auto-escalates to CDP; requires the debugger capability -- check
    browser_devices()'s capabilities.debugger first). Without it, only the active tab
    of a focused window can be captured -- fails loud rather than silently activating
    the tab. On Android (no CDP), only the active tab can ever be captured.

    frame_id crops the capture to one frame's own on-screen region (from a prior
    browser_snapshot/browser_read's `frames` entries) -- e.g. a document viewer
    embedded in an iframe, when you don't want the whole tab. Requires capture_hidden.

    multi_page=True scrolls and captures repeatedly (up to max_pages, default 10,
    hard cap 50) until the scrollable region's end is reached -- for content that
    doesn't fit in one viewport (e.g. a multi-page document viewer). Returns one
    image per page plus honest metadata: page_count, capped (True if max_pages was
    hit before the real end), stopped_reason. scroll_selector targets a specific
    scrollable container (CSS selector) instead of the page's own scroll; page_delay_ms
    is the settle delay between scroll and capture.

    If the device is not 'live', this returns the hub's {"status": "queued", ...}
    dict UNCHANGED (as JSON text, not an image) -- that is a normal, actionable
    result, not an error or a hang -- call browser_poll(device_id, command_id) later.
    """
    args: dict[str, Any] = {}
    if capture_hidden:
        args["capture_hidden"] = True
    if frame_id is not None:
        args["frame_id"] = frame_id
    if multi_page:
        args["multi_page"] = True
        args["max_pages"] = max_pages if max_pages is not None else 10
    if scroll_selector is not None:
        args["scroll_selector"] = scroll_selector
    if page_delay_ms is not None:
        args["page_delay_ms"] = page_delay_ms
    result = await _run_command(
        device_id, "screenshot", args, window_id=window_id, tab_id=tab_id, timeout_s=timeout_s
    )
    if not result.get("ok"):
        return result  # queued/error -- nothing to render as an image
    payload = result["result"]
    if isinstance(payload, dict) and "pages" in payload:
        images = [Image(data=base64.b64decode(p["base64"]), format="jpeg") for p in payload["pages"]]
        meta = {k: v for k, v in payload.items() if k != "pages"}
        return [*images, meta]
    if not isinstance(payload, dict) or "base64" not in payload:
        return result
    image = Image(data=base64.b64decode(payload["base64"]), format="jpeg")
    meta = {k: v for k, v in payload.items() if k != "base64"}
    return [image, meta]


@mcp.tool()
async def browser_vision_read(
    device_id: str,
    tab_id: int,
    prompt: str | None = None,
    window_id: int | None = None,
    frame_id: int | None = None,
    multi_page: bool = False,
    max_pages: int | None = None,
    scroll_selector: str | None = None,
    page_delay_ms: int | None = None,
    capture_hidden: bool = True,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Capture pixels and extract TEXT from them via a vision-capable LLM -- a real,
    separate model-call mechanism, distinct from browser_screenshot (which only
    returns pixels and never calls a model). Use this when the content you need was
    never present in the DOM as text (e.g. a canvas-rendered document viewer, like
    Word/PowerPoint Online) and you want text back in context rather than an image
    your own model would need to look at.

    Requires a vision provider configured via environment variable on the machine
    running this MCP server (ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY, or
    ABB_VISION_PROVIDER to pin a specific one) -- fails loud with setup instructions
    (as {"ok": false, "error": ...}) if none is configured; never silently returns
    empty text.

    capture_hidden defaults to True here (unlike browser_screenshot) -- this tool
    exists specifically to reach tabs you shouldn't activate just to look at.
    frame_id/multi_page/max_pages/scroll_selector/page_delay_ms mean exactly what
    they mean on browser_screenshot -- see that tool's description.

    Returns {"ok": true, "result": {"text": ..., "vision_provider": ..., "vision_model":
    ..., "image_count": ..., "page_count": ..., "capped": ..., "stopped_reason": ...}}
    on success, or the hub's own queued/error shape if the underlying screenshot
    capture itself was queued or failed (the vision model is never called without a
    real captured image in hand).
    """
    target = Target(device_id=device_id, window_id=window_id, tab_id=tab_id)
    try:
        return await _vision_read(
            _client(),
            target,
            prompt=prompt,
            frame_id=frame_id,
            multi_page=multi_page,
            max_pages=max_pages,
            scroll_selector=scroll_selector,
            page_delay_ms=page_delay_ms,
            capture_hidden=capture_hidden,
            timeout_s=timeout_s,
        )
    except (VisionConfigError, VisionError) as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def browser_wait_for(
    device_id: str,
    tab_id: int,
    selector: str,
    timeout_ms: int = 10000,
    window_id: int | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Poll (never sleep blindly) until a CSS selector matches an element, or time
    out after timeout_ms. Runs against the top frame only in this phase -- a
    selector that only exists inside an iframe will not be found (see
    docs/PROTOCOL.md's "Frames" section).

    `timeout_s`, if given, overrides the hub's device-round-trip wait -- it must be
    at least as large as timeout_ms/1000, or the hub will give up on the round trip
    before the in-page poll finishes (see docs/PROTOCOL.md's "Command timeout").

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
        timeout_s=timeout_s,
    )


@mcp.tool()
async def browser_wait_text(
    device_id: str,
    tab_id: int,
    text: str,
    timeout_ms: int = 10000,
    window_id: int | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Poll (never sleep blindly) until the tab's visible text contains a
    substring, or time out after timeout_ms. Runs against the top frame only in
    this phase (see docs/PROTOCOL.md's "Frames" section).

    `timeout_s`, if given, overrides the hub's device-round-trip wait -- it must be
    at least as large as timeout_ms/1000, or the hub will give up on the round trip
    before the in-page poll finishes (see docs/PROTOCOL.md's "Command timeout").

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
        timeout_s=timeout_s,
    )


@mcp.tool()
async def browser_fetch_bytes(
    device_id: str, url: str, max_bytes: int | None = None, timeout_s: float | None = None
) -> dict[str, Any]:
    """Fetch a URL from the EXTENSION's own context, with credentials included -- rides the
    user's real authenticated session (cookies) for the target origin. No tab_id needed. This
    is the mechanism for retrieving a linked file (.docx/.pdf/binary) that a page only links
    to, using the user's existing login -- distinct from browser_read/browser_snapshot, which
    only ever see text already present in the DOM (a canvas-rendered document, e.g. a Word
    Online viewer, has NO document text in the DOM at all -- this is the way to get its
    content). Returns {url, content_type, byte_length, base64} on success.

    Refuses (with a clear error naming the limit) past a byte-size cap -- pass max_bytes to
    raise it (default 25MB). If the target blocks extension-context requests (some CDNs/hotlink
    protection check the request's Referer/Origin), browser_grab_image fetches from the PAGE's
    own script context instead, carrying the page's real Referer -- try that if this fails with
    an HTTP error.

    Device-only target (no tab_id).

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    args: dict[str, Any] = {"url": url}
    if max_bytes is not None:
        args["max_bytes"] = max_bytes
    return await _run_command(device_id, "fetch_bytes", args, timeout_s=timeout_s)


@mcp.tool()
async def browser_grab_image(
    device_id: str,
    tab_id: int,
    url: str,
    window_id: int | None = None,
    max_bytes: int | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Fetch a URL from the PAGE's own main-world script context (not the extension's) -- the
    request carries the page's own Referer and cookie context, which defeats hotlink/Referer
    protection an extension-context fetch (browser_fetch_bytes) would trip. Requires a tab_id
    (the page whose script context does the fetching) -- browser_fetch_bytes has no such
    requirement and is simpler when the URL doesn't need page-Referer context; use this one
    specifically when that one fails with an HTTP error, or you already know the target needs
    the page's own session context.

    Returns {url, content_type, byte_length, base64} on success. Refuses (naming the limit)
    past a byte-size cap -- pass max_bytes to raise it (default 25MB).

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    args: dict[str, Any] = {"url": url}
    if max_bytes is not None:
        args["max_bytes"] = max_bytes
    return await _run_command(
        device_id, "grab_image", args, window_id=window_id, tab_id=tab_id, timeout_s=timeout_s
    )


@mcp.tool()
async def browser_downloads_list(
    device_id: str, limit: int = 20, timeout_s: float | None = None
) -> dict[str, Any]:
    """List recent downloads on a device (chrome.downloads.search), plus max_download_id -- the
    highest download id chrome currently knows about. Call this BEFORE an action that triggers
    a native/indirect download (e.g. clicking a page's own Download control) and pass its
    max_download_id as browser_wait_download's since_id, so the new download is identified
    without ever mistaking one the human started themselves for the agent's own.

    Device-only target.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    return await _run_command(device_id, "downloads_list", {"limit": limit}, timeout_s=timeout_s)


@mcp.tool()
async def browser_download(
    device_id: str, url: str, filename: str | None = None, timeout_s: float | None = None
) -> dict[str, Any]:
    """Trigger a download of a URL directly (chrome.downloads.download) -- returns a
    download_id you already know precisely, since this command started the download itself.
    Pass it to browser_wait_download's download_id to poll for completion.

    Device-only target.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    args: dict[str, Any] = {"url": url}
    if filename is not None:
        args["filename"] = filename
    return await _run_command(device_id, "download", args, timeout_s=timeout_s)


@mcp.tool()
async def browser_wait_download(
    device_id: str,
    download_id: int | None = None,
    since_id: int | None = None,
    pattern: str | None = None,
    timeout_ms: int = 30000,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Poll (never sleep blindly) for a completed download. Exactly one of download_id (from a
    prior browser_download call) or since_id (a baseline max_download_id from
    browser_downloads_list, taken BEFORE an action that triggers an indirect download -- e.g.
    clicking a page's own Download control) is required. since_id mode never matches a
    download at or below the baseline, so it structurally cannot claim a download the human
    started themselves. pattern (optional regex) narrows a since_id search by filename.

    Returns {download_id, filename, url, mime, byte_length, state} once complete, or an error
    if the download was interrupted (failed) or the timeout_ms deadline passed first.

    Device-only target.

    If the device is not 'live', this returns immediately as {"status": "queued",
    "command_id": ..., "tier": ..., "last_seen": ..., "queue_position": ...}
    instead of {"ok": ...}. That is a normal, actionable result, not an error or a
    hang -- call browser_poll(device_id, command_id) later to retrieve the
    eventual result.
    """
    if download_id is None and since_id is None:
        return {"ok": False, "error": "browser_wait_download requires download_id or since_id"}
    args: dict[str, Any] = {"timeout_ms": timeout_ms}
    if download_id is not None:
        args["download_id"] = download_id
    if since_id is not None:
        args["since_id"] = since_id
    if pattern is not None:
        args["pattern"] = pattern
    return await _run_command(device_id, "wait_download", args, timeout_s=timeout_s)


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
