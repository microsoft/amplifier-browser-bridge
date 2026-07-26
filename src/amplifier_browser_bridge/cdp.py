"""CDP (Chrome DevTools Protocol) escalation: hub-side attach/detach bookkeeping.

Per design doc §7 ("CDP: opt-in, not default"): injection-only (`chrome.scripting`
+ `injected.js`) is the default posture for every command. CDP is an *enhancement*
-- trusted input (`isTrusted: true`) and any-tab/hidden-tab screenshot capture --
escalated to **per-tab**, **on demand**, **never speculatively**.

This module owns exactly the state a hub needs to make that policy real:

    - which (device_id, tab_id) pairs currently have a live `chrome.debugger`
      session, per the hub's own bookkeeping (never trusted from a caller --
      same capability-binding discipline as policy.py's denylist);
    - how long a session has sat idle since its last CDP-requiring use, so the
      hub can soft-detach it (design doc §6.3/§7: "so the banner clears while
      the human is just browsing").

The actual `chrome.debugger.attach`/`sendCommand`/`detach` calls happen entirely
inside the extension (background.js) -- this module has no browser connection of
its own. `hub.py` is what wires this state machine to real dispatch: see its
`_ensure_cdp_attached`, `_dispatch_live`, and `soft_detach_idle_tabs`.

## Why attach state lives here, not on `DeviceRecord` (registry.py)

`DeviceRecord` is one record per *device*. CDP attach is scoped per *tab* within
a device, and (unlike device identity) is inherently transient -- it must reset
to "not attached" on every reconnect (a fresh service worker has no live
`chrome.debugger` sessions; see `chrome.debugger` semantics: attaching is a
property of a specific renderer target, not something that survives an
extension restart). Keeping it in a separate registry, reset independently of
`DeviceRecord.bind()`, avoids conflating "the device is known" with "a CDP
session happens to be live right now" -- the same reasoning that keeps
`DeviceCommandQueue` a separate class from `DeviceRecord` (queue.py).

## Fail loud, never silently degrade (design doc §8)

A command that genuinely needs CDP (trusted input, hidden-tab capture) and
lands on a device without `chrome.debugger` (Edge Android -- design doc §2/§7:
"chrome.debugger ... genuinely absent") must return a clear
"capability unavailable on this device" error. It must NOT silently fall back
to the injection-only path the caller didn't ask for -- that would mean, e.g.,
a caller who explicitly asked for `isTrusted: true` input getting an untrusted
synthetic click back with no indication anything different happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .args_bool import truthy

# 20 seconds. Revised down from 600s after real-world use on a human's live
# browser.
#
# The original 10-minute value optimised for "don't let the banner flicker
# between commands" and assumed the banner was a minor cosmetic artifact. That
# assumption was wrong. The Edge debugger infobar is persistent, occupies real
# vertical screen space, and pushes page content down for as long as it is up.
# Ten minutes of that after a single CDP-requiring command is a serious
# imposition on someone who is trying to work in that browser -- a direct
# violation of the co-working etiquette in design doc §6.3.
#
# 20s is long enough to absorb a burst of CDP commands without re-raising the
# banner between them, and short enough that the banner disappears on its own
# very shortly after the agent stops. Configurable per-Hub (see hub.py's
# `cdp_idle_seconds` constructor arg) -- this is a default, not a mandate.
DEFAULT_SOFT_DETACH_IDLE_SECONDS: float = 20.0

# Args that express CALLER INTENT for CDP-backed dispatch (see protocol.py's
# CDP_INTENT_ARGS, kept in sync here as the single source of truth for what
# "requires CDP" means). Deliberately declarative ("I need this to be
# trusted" / "I need this even though the tab isn't active"), never a raw
# `_cdp` on/off switch -- that flag is hub-internal (see hub.py's
# `send_command`, which strips any caller-supplied `_cdp` before evaluation).
_TRUSTED_INPUT_COMMANDS: frozenset[str] = frozenset({"click", "type", "key"})


def requires_cdp(command: str, args: dict[str, Any]) -> bool:
    """True if this (command, args) pair genuinely needs CDP to satisfy the
    caller's expressed intent -- the single decision point for "automatic
    escalation" (design doc §7). Never true merely because CDP happens to be
    available; only because the caller asked for something injection-only
    cannot provide:

        - `click`/`type`/`key` with `args["trusted"] is True`: the caller
          needs `isTrusted: true` events, which `injected.js`'s
          `dispatchEvent` calls structurally cannot produce.
        - `screenshot` with `args["capture_hidden"] is True`: the caller
          needs to capture a tab that may not be the active tab of a focused
          window, which `chrome.tabs.captureVisibleTab` cannot do (design doc
          §7: "screenshotting a non-active tab is desktop-only [via CDP]").
    """
    if command in _TRUSTED_INPUT_COMMANDS and truthy(args.get("trusted")):
        return True
    return command == "screenshot" and truthy(args.get("capture_hidden"))


@dataclass
class TabCdpState:
    """CDP bookkeeping for one (device, tab) pair."""

    attached: bool = False
    attached_at: datetime | None = None
    last_activity: datetime | None = None
    # Set on every detach (requested, soft-detach-idle, or unsolicited --
    # Cancel on the banner, DevTools opened, target crashed/discarded). Surfaced
    # to agents via `devices`/`tabs` so a caller can reason about *why* a
    # session it expected to be attached is not (design doc §8: "surface real
    # errors; recover by re-attaching where sensible").
    last_detach_reason: str | None = None

    def touch(self, *, now: datetime | None = None) -> None:
        self.last_activity = now or datetime.now(UTC)

    def is_idle(self, idle_seconds: float, *, now: datetime | None = None) -> bool:
        if not self.attached or self.last_activity is None:
            return False
        now = now or datetime.now(UTC)
        return (now - self.last_activity).total_seconds() >= idle_seconds

    def to_summary(self) -> dict[str, Any]:
        return {
            "attached": self.attached,
            "attached_at": self.attached_at.isoformat() if self.attached_at else None,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "last_detach_reason": self.last_detach_reason,
        }


class CdpRegistry:
    """Independent per-(device, tab) CDP state -- the same structural
    principle as `DeviceRegistry` (registry.py): no global "the attached tab"
    slot. Every method is a plain synchronous state transition; `hub.py` is
    responsible for actually sending `attach`/`detach` commands to a device
    and calling these methods once a result/event confirms the real state.
    """

    def __init__(self, idle_seconds: float = DEFAULT_SOFT_DETACH_IDLE_SECONDS) -> None:
        self.idle_seconds = idle_seconds
        self._states: dict[tuple[str, int], TabCdpState] = {}

    def _get_or_create(self, device_id: str, tab_id: int) -> TabCdpState:
        key = (device_id, tab_id)
        state = self._states.get(key)
        if state is None:
            state = TabCdpState()
            self._states[key] = state
        return state

    def mark_attached(self, device_id: str, tab_id: int, *, now: datetime | None = None) -> None:
        state = self._get_or_create(device_id, tab_id)
        now = now or datetime.now(UTC)
        state.attached = True
        state.attached_at = now
        state.last_activity = now
        state.last_detach_reason = None

    def mark_detached(self, device_id: str, tab_id: int, *, reason: str | None = None) -> None:
        state = self._states.get((device_id, tab_id))
        if state is None:
            # Detaching something the hub never recorded as attached is a
            # harmless no-op (e.g. a `detach` sent defensively, or an
            # unsolicited event for a tab this hub instance never attached
            # to) -- create the record anyway so the reason is discoverable.
            state = self._get_or_create(device_id, tab_id)
        state.attached = False
        state.last_detach_reason = reason

    def touch(self, device_id: str, tab_id: int, *, now: datetime | None = None) -> None:
        """Reset the idle clock -- called on every CDP-*requiring* dispatch
        (attach itself, or a trusted click/hidden screenshot once attached).
        Deliberately NOT called for ordinary (non-CDP) commands against an
        attached tab: soft-detach should reclaim the banner as soon as the
        agent stops needing CDP specifically, even if it's still issuing
        plain injection-only commands against the same tab."""
        if (device_id, tab_id) in self._states:
            self._get_or_create(device_id, tab_id).touch(now=now)

    def is_attached(self, device_id: str, tab_id: int) -> bool:
        state = self._states.get((device_id, tab_id))
        return bool(state and state.attached)

    def idle_tabs(self, *, now: datetime | None = None) -> list[tuple[str, int]]:
        """(device_id, tab_id) pairs currently attached and idle past the
        configured threshold -- exactly what `Hub.soft_detach_idle_tabs`
        acts on."""
        now = now or datetime.now(UTC)
        return [key for key, state in self._states.items() if state.is_idle(self.idle_seconds, now=now)]

    def snapshot(self, device_id: str) -> dict[int, dict[str, Any]]:
        """Per-tab CDP state for one device -- merged into `devices`
        responses (see `Hub._devices_snapshot`) so an agent can reason about
        attach state without a dedicated round trip."""
        return {
            tab_id: state.to_summary() for (dev, tab_id), state in self._states.items() if dev == device_id
        }
