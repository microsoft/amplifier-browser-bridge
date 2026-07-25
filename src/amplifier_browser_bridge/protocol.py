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
        "tabs",
        "tab_open",
        "tab_close",
        "tab_activate",
        "screenshot",
        "wait_for",
        "wait_text",
    }
)

# Commands that operate purely inside the page (dispatched into injected.js).
# The rest are browser-chrome-level commands the extension's background script
# executes directly against chrome.tabs / chrome.windows.
PAGE_WORLD_COMMANDS: frozenset[str] = frozenset(
    {"snapshot", "read", "click", "type", "key", "scroll", "back", "forward", "wait_for", "wait_text"}
)
BROWSER_LEVEL_COMMANDS: frozenset[str] = COMMANDS - PAGE_WORLD_COMMANDS

# ---------------------------------------------------------------------------
# Message type vocabularies
# ---------------------------------------------------------------------------

DEVICE_TO_HUB_TYPES: frozenset[str] = frozenset({"hello", "heartbeat", "result", "event"})
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
