"""Canonical message shapes for the Amplifier Browser Bridge wire protocol.

Two related but distinct vocabularies share one JSON envelope:

1. **Device protocol** -- extension <-> hub, over the hub's ``/device`` WebSocket route.
2. **Agent protocol**  -- CLI/lib <-> hub, over the hub's ``/agent`` WebSocket route.

Both use the same envelope shape::

    {"v": PROTOCOL_VERSION, "id": "<uuid4>", "type": "<message type>", ...fields}

``id`` is the correlation id. A request and its eventual response/result share the same
``id`` so callers can match them without additional bookkeeping.

See ``docs/PROTOCOL.md`` for the full narrative spec with example payloads for every
message type. This module is the single source of truth for the *names* used in that
spec (message types, command vocabulary, capability keys) -- keep the two in sync.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

PROTOCOL_VERSION = 1

# ---------------------------------------------------------------------------
# Command vocabulary
# ---------------------------------------------------------------------------
# Deliberately mirrors Playwright MCP's tool names (snapshot/click/type/navigate/
# tabs/wait_*) -- models already expect these names, and there is no value in
# inventing new ones. Every command here is implemented by the extension via
# chrome.scripting.executeScript only (no CDP in this phase -- see design doc §7).

COMMANDS: frozenset[str] = frozenset(
    {
        "snapshot",
        "click",
        "type",
        "key",
        "scroll",
        "navigate",
        "back",
        "forward",
        "read",
        # D1 (docs/designs/confirmation-gate.md section 11.5): on-demand full
        # action descriptor for one ref -- exists for the `unknown`
        # classification recovery path (reason_code "ref_not_observed"), so a
        # caller can obtain a descriptor without a full re-snapshot. Never
        # auto-fires; the caller invokes it explicitly.
        "describe",
        "tabs",
        "tab_open",
        "tab_close",
        "tab_activate",
        "screenshot",
        "wait_for",
        "wait_text",
        # CDP escalation (Phase 4, design doc §7) -- explicit attach/detach.
        # BROWSER_LEVEL_COMMANDS (handled by background.js against
        # chrome.debugger directly; see cdp.py for the hub-side state
        # machine and docs/PROTOCOL.md for the wire shape).
        "attach",
        "detach",
        # Phase 5 (real-profile hardening): explicit extension self-reload,
        # so an unpacked-extension update on the browser device is
        # self-service from an already-connected agent surface instead of
        # requiring a manual click in edge://extensions every iteration --
        # see docs/PROTOCOL.md and background.js's `reloadExtension()`.
        # Device-only target, like `tab_open`; no tab_id involved.
        "reload",
        # Content-extraction mechanisms (design doc's "Mechanism, not policy"
        # section). Each is a DISTINCT extraction strategy with different
        # tradeoffs -- the caller picks, this layer never substitutes one for
        # another or silently escalates between them:
        #
        #   fetch_bytes    -- extension-context fetch, credentials: "include"
        #                     (rides the user's cookies for the target origin).
        #                     Device-only target; see background.js's fetchBytes().
        #   grab_image     -- fetch from the PAGE's own MAIN-world script context,
        #                     so the request carries the page's real Referer/cookie
        #                     context -- defeats hotlink protection an
        #                     extension-context fetch would trip. Needs a tab_id.
        #   downloads_list -- chrome.downloads.search: recent entries + a precise
        #                     max_download_id baseline. Device-only target.
        #   download       -- chrome.downloads.download: trigger a download
        #                     directly, returning ITS OWN definite download_id.
        #                     Device-only target.
        #   wait_download  -- poll (never sleep) for a completed download, either
        #                     a specific download_id or a NEW one after a baseline
        #                     since_id (+ optional filename pattern) -- the
        #                     baseline+pattern approach so a download the human
        #                     started themselves is never claimed as the agent's.
        #                     Device-only target.
        "fetch_bytes",
        "grab_image",
        "downloads_list",
        "download",
        "wait_download",
        # Browser-state archive capability (design doc's "Mechanism, not
        # policy" section still applies -- each of these is a distinct,
        # named capture mechanism the ARCHIVE ORCHESTRATOR (archive.py,
        # hub-side Python) composes; none is exposed as its own agent-facing
        # tool in this phase -- see archive.py's module docstring for why
        # (deep-capture payloads must go hub-side -> disk, never back
        # through an agent tool's return value).
        #
        #   windows        -- BROWSER_LEVEL. chrome.windows.getAll +
        #                     chrome.tabGroups.query: full window metadata
        #                     (incl. incognito) and tab-group metadata. No
        #                     tab_id/page contact at all -- the L0 (cheapest)
        #                     rung of the archive depth ladder.
        #   page_state     -- PAGE_WORLD (injected.js). Per-tab outerHTML,
        #                     form field values (password values excluded,
        #                     always), localStorage/sessionStorage, and
        #                     scroll position. Top frame only by default,
        #                     same documented narrower limitation as
        #                     scroll/wait_for/etc ("Frames" section) --
        #                     args.frame_id targets one known frame.
        #   mhtml          -- BROWSER_LEVEL, CDP-only (Page.captureSnapshot).
        #                     No injection-only alternative exists at all --
        #                     see cdp.py's requires_cdp, which treats this
        #                     command as unconditionally CDP-requiring.
        #   nav_history    -- BROWSER_LEVEL, CDP-only
        #                     (Page.getNavigationHistory). Same
        #                     unconditional-CDP treatment as mhtml.
        #   history_list, bookmarks_list, sessions_list, top_sites,
        #   reading_list   -- BROWSER_LEVEL, device-only target. Browser-wide
        #                     profile data (chrome.history/bookmarks/
        #                     sessions/topSites/readingList) -- not scoped to
        #                     any one tab.
        #   cookies_list   -- BROWSER_LEVEL, device-only target.
        #                     chrome.cookies.getAll. Deliberately NOT gated
        #                     at the wire-command level (a caller invoking
        #                     this command directly gets cookies, same as
        #                     any other command) -- the opt-in gate lives at
        #                     the ARCHIVE ORCHESTRATOR level (archive.py's
        #                     `include_cookies`, default False, never
        #                     included even at the deepest archive level
        #                     unless explicitly requested).
        "windows",
        "page_state",
        "mhtml",
        "nav_history",
        "history_list",
        "bookmarks_list",
        "sessions_list",
        "top_sites",
        "reading_list",
        "cookies_list",
    }
)

# Commands that operate purely inside the page (dispatched into injected.js).
# The rest are browser-chrome-level commands the extension's background script
# executes directly against chrome.tabs / chrome.windows / chrome.debugger.
PAGE_WORLD_COMMANDS: frozenset[str] = frozenset(
    {
        "snapshot",
        "describe",
        "read",
        "click",
        "type",
        "key",
        "scroll",
        "back",
        "forward",
        "wait_for",
        "wait_text",
        # Archive capability (see COMMANDS' comment above): per-tab
        # outerHTML/forms/storage/scroll, dispatched into injected.js like
        # every other page-world command. Top frame only by default -- same
        # documented narrower limitation as scroll/wait_for/wait_text.
        "page_state",
    }
)
BROWSER_LEVEL_COMMANDS: frozenset[str] = COMMANDS - PAGE_WORLD_COMMANDS

# Optional args that opt a PAGE_WORLD_COMMAND into CDP-backed dispatch instead
# of injected.js's synthetic events -- see cdp.py's `requires_cdp`. Declarative
# intent from the caller ("I need this to be isTrusted" / "I need this tab even
# though it's not active"), never a raw CDP on/off switch -- the hub decides
# HOW to satisfy the request (attach bookkeeping, capability check) and is the
# only writer of the wire-level `_cdp` flag the device actually acts on.
CDP_INTENT_ARGS: frozenset[str] = frozenset({"trusted", "capture_hidden"})

# Optional args recognized ONLY by the hub, on ANY command -- unlike
# CDP_INTENT_ARGS (which describe caller intent that the hub translates into a
# wire-level signal the DEVICE acts on), these never reach the device at all.
# `Hub.send_command` pops them from `args` before a `QueuedCommand` is built
# (see hub.py's module docstring, "single choke point") -- the extension has no
# code path that reads them, so they never appear on the `/device` route wire.
#
# `timeout_s` (float, seconds): overrides the hub's default wait for THIS
# device round trip only -- see hub.py's DEFAULT_COMMAND_TIMEOUT and
# MIN/MAX_COMMAND_TIMEOUT for the accepted range. Real-world finding that
# motivated this: a heavy SPA (repos.opensource.microsoft.com's Open Source
# Management Portal) needed noticeably longer than the prior fixed 30s to
# finish injection + traversal even once `status: "complete"` -- see
# docs/PROTOCOL.md's "Command timeout" section.
HUB_ONLY_ARGS: frozenset[str] = frozenset({"timeout_s"})

# ---------------------------------------------------------------------------
# WebSocket message-size ceiling
# ---------------------------------------------------------------------------
# Neither WebSocket library this codebase depends on was ever asked, on this
# protocol's behalf, how big a single message is allowed to be -- each was
# left on its own generic default:
#
#   - `websockets` (the CLIENT's library -- client.py's `websockets.connect`)
#     defaults `max_size` to 2**20 (1,048,576) bytes.
#   - `aiohttp` (the HUB's library -- hub.py's two `web.WebSocketResponse()`
#     routes, `/device` and `/agent`) defaults `max_msg_size` to
#     4 * 1024 * 1024 (4MB).
#
# Real-world finding: archiving four real web pages at MHTML depth (L4 --
# `Page.captureSnapshot`, archive.py) died with `websockets`' own "sent 1009
# (message too big) frame exceeds limit of 1048576 bytes" -- the CLIENT
# tripping its unset (so default) 1MB cap while receiving the hub's relayed
# `mhtml` result. A real page's MHTML inlines every stylesheet, font, and
# image it references and routinely lands well past 1MB -- sometimes past
# 4MB too -- for a genuinely heavy page (github.com, huggingface.co); the
# earlier MHTML testing that never hit this only used 30-62KB local test
# decks, nowhere near either default.
#
# One shared, EXPLICIT, BOUNDED ceiling, applied identically to all three
# legs of a round trip (device -> hub, hub -> agent, and the agent/CLI
# client's own receive) -- so no leg silently enforces a smaller limit than
# another, and a payload that clears one hop only to be rejected by the next
# is not a failure mode this protocol has to reason about. Deliberately NOT
# `None`/unbounded: an unbounded per-message size is an unbounded per-command
# memory allocation on both the hub and the client, for a payload size the
# caller cannot predict or cap themselves. 64MiB is comfortably past any real
# MHTML capture observed in practice while still being a real ceiling -- a
# capture that manages to exceed even this fails that ONE capture (see
# archive.py's `_safe_command`), never the whole archive run, and never
# silently (docs/PROTOCOL.md's "WebSocket message-size ceiling" section).
MAX_WS_MESSAGE_BYTES: int = 64 * 1024 * 1024

# ---------------------------------------------------------------------------
# Message type vocabularies
# ---------------------------------------------------------------------------

# `capabilities_update` (Phase 4): an unsolicited, post-`hello` correction to a
# device's capability set -- see cdp.py/background.js's re-probe logic and
# docs/PROTOCOL.md. Needed because some capabilities (capture_visible_tab,
# scripting) are only truthfully probeable once a real tab exists, which is not
# guaranteed at `hello` time (Phase 1 finding: a fresh browser launch can have
# zero tabs).
DEVICE_TO_HUB_TYPES: frozenset[str] = frozenset(
    {"hello", "heartbeat", "result", "event", "capabilities_update"}
)
HUB_TO_DEVICE_TYPES: frozenset[str] = frozenset({"command", "ping", "error"})

AGENT_TO_HUB_TYPES: frozenset[str] = frozenset({"list_devices", "command", "poll"})
HUB_TO_AGENT_TYPES: frozenset[str] = frozenset({"devices", "result", "error"})

# ---------------------------------------------------------------------------
# Capability keys
# ---------------------------------------------------------------------------
# Reported by the extension in `hello.capabilities`, from a BEHAVIORAL probe (real
# invocation in a try/catch) -- never a `typeof` check. See design doc §7 and §8.

CAPABILITY_KEYS: tuple[str, ...] = (
    "scripting",
    "windows",
    "tab_groups",
    "debugger",
    "capture_visible_tab",
    "downloads",
    "storage",
    "alarms",
    # Archive capability (see COMMANDS' comment): browser-wide profile-data
    # permissions, each a real behavioral probe in background.js's
    # probeCapabilities() -- never a `typeof` check. `debugger` (above)
    # already gates the two CDP-only capture commands (mhtml, nav_history);
    # these five gate the profile-data commands the archive orchestrator's
    # deepest level (L5) collects. Unlike `debugger`, none of these is known
    # to be genuinely absent on Edge Android -- manifest.android.json
    # requests all five.
    "history",
    "bookmarks",
    "sessions",
    "top_sites",
    "reading_list",
    # `cookies` is reported the same behavioral way as every other capability
    # here -- capability reporting is never the gate. The actual opt-in gate
    # for cookie collection lives at the archive orchestrator level
    # (archive.py's `include_cookies`, default False) -- see COMMANDS'
    # comment on `cookies_list`.
    "cookies",
)


def new_id() -> str:
    """Generate a correlation id. Plain uuid4 -- no meaning encoded, just uniqueness."""
    return str(uuid.uuid4())


def now_iso() -> str:
    """UTC timestamp in ISO-8601, used consistently across audit log and protocol fields."""
    return datetime.now(UTC).isoformat()


def envelope(msg_type: str, /, msg_id: str | None = None, **fields: Any) -> dict[str, Any]:
    """Build a protocol envelope. `msg_id` defaults to a fresh correlation id."""
    return {"v": PROTOCOL_VERSION, "id": msg_id or new_id(), "type": msg_type, **fields}
