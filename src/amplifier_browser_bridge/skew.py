"""Command-set skew detection between hub and device (Tier 0 handshake).

## The problem this replaces

Before this module existed, there was ZERO version awareness of the extension
anywhere. `sendHello()` sent a hardcoded `protocol_version: 1` constant and no
manifest version at all; `DeviceRecord` had nowhere to store one; the hub
stored `protocol_version` and never compared it to anything. An extension
that couldn't execute a command simply fell through `executeCommand()`'s own
if-chain to a bare `{"ok": false, "error": "unsupported command: X"}` -- no
hint that the extension was stale, which is the common case and useless.

Version strings don't fix this: they're hand-maintained and already measured
to drift in this exact repo (`pyproject.toml` said `0.1.0`, `manifest.json`
said `0.4.0`, same repo, same day, no CI). A self-reported *command set*
cannot drift the same way, because it's derived from what the extension
actually just told the hub it can do (`hello.commands` --
`extension/background.js`'s `SUPPORTED_COMMANDS`), not from a number someone
forgot to bump.

## Bidirectional, always naming the side

`protocol.COMMANDS` is the hub's own vocabulary. Comparing it against a
device's self-reported `commands` produces two DIFFERENT findings that need
DIFFERENT fixes:

- The device is missing commands the hub knows (`device_behind`) -- the
  common case: an unupdated extension. Fix: update the extension.
- The device reports commands the hub doesn't recognize (`hub_behind`) --
  the extension is newer than this hub's own code. Fix: update/restart the
  hub.

Collapsing these into one boolean ("in sync: yes/no") would erase exactly the
information a caller needs to know what to actually do.

## The pre-handshake case

A device that has never sent a `commands` field in `hello` at all -- every
extension shipped before this feature -- is not "unknown" in the sense of
"we can't tell," and it is not a crash. It is a DEFINITIVELY STALE extension:
`SkewReport.known` is `False`, and `describe_skew` names this plainly rather
than reporting a spurious "missing: [...]" list (which would incorrectly
imply the hub has positive knowledge of exactly what's absent, when really it
has none). This is the FIRST skew case every existing deployment hits the
moment this ships, including the maintainer's own currently-connected
browser -- see `update_extension.py` for how the update tool treats it (still
attempts the automatic path; a pre-handshake device transitioning to a real,
non-empty `commands` set after a reload IS the verification signal that the
automatic update worked).

## Deliberately NOT a dispatch-blocking check for the unknown case

`capability_error` (used by `hub.py`'s `send_command` as the fast-fail this
whole feature exists to enable) only refuses a command when the device has
POSITIVELY reported a command set that excludes it (`known=True`). A device
with an unknown command set (`known=False`, i.e. every currently-deployed
pre-Tier-0 extension) is dispatched exactly as before this feature shipped --
`capability_error` returns `None` for it. This is deliberate, not an
oversight: if it blocked dispatch for every command to every pre-Tier-0
device, it would also block the ONE command the entire update story depends
on -- `reload` -- for every currently-connected browser the moment this
ships, since none of them have reported a command set yet. The hub simply
has no positive evidence such a device *can't* run a given command, so it
does not refuse one; staleness for these devices is instead surfaced
passively, everywhere an agent would look before acting (`devices` listings,
`browser_devices` -- see `SkewReport.to_summary()` and `hub.py`'s
`_devices_snapshot()`), not by failing commands that may well still work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# SkewReport -- the bidirectional comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkewReport:
    """`known=False` means the device has never reported a command set at all
    (pre-Tier-0 extension, or one that hasn't reconnected since updating) --
    a distinct, definitively-stale state, not merely "in_sync=False" with an
    empty diff. `device_behind`/`hub_behind` are always empty when
    `known=False`: there is nothing to diff a device's commands against when
    it never reported any."""

    known: bool
    device_behind: frozenset[str]
    hub_behind: frozenset[str]

    @property
    def in_sync(self) -> bool:
        return self.known and not self.device_behind and not self.hub_behind

    def to_summary(self) -> dict[str, Any]:
        """JSON-friendly shape, attached to every `devices`/`browser_devices`
        entry (`hub.py`'s `_devices_snapshot`) -- so an agent can see a stale
        device BEFORE ever failing a command against it, not only after."""
        return {
            "known": self.known,
            "in_sync": self.in_sync,
            "device_behind": sorted(self.device_behind),
            "hub_behind": sorted(self.hub_behind),
            "summary": describe_skew(self),
        }


def compute_skew(device_commands: frozenset[str] | None, hub_commands: frozenset[str]) -> SkewReport:
    """Compare one device's self-reported `commands` (from `hello`, or `None`
    if it never reported any) against this hub's own vocabulary
    (`protocol.COMMANDS`)."""
    if device_commands is None:
        return SkewReport(known=False, device_behind=frozenset(), hub_behind=frozenset())
    return SkewReport(
        known=True,
        device_behind=frozenset(hub_commands) - frozenset(device_commands),
        hub_behind=frozenset(device_commands) - frozenset(hub_commands),
    )


def describe_skew(report: SkewReport) -> str | None:
    """One-line, actionable summary of `report`, or `None` when there is
    nothing to report (in sync). Always names WHICH side is behind."""
    if report.in_sync:
        return None
    if not report.known:
        return (
            "this extension has never reported a command set -- a definitively stale "
            "extension (pre-Tier-0 build, or one that hasn't reconnected since updating). "
            "Update it (browser_update_extension), then reconnect."
        )
    parts: list[str] = []
    if report.device_behind:
        parts.append(
            f"the EXTENSION is behind this hub -- missing {sorted(report.device_behind)}; "
            "update the extension (browser_update_extension)"
        )
    if report.hub_behind:
        parts.append(
            f"the HUB is behind this extension -- does not recognize {sorted(report.hub_behind)} "
            "it reported; update/restart the hub"
        )
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Dispatch-time fast-fail
# ---------------------------------------------------------------------------


def capability_error(command: str, device_commands: frozenset[str] | None) -> dict[str, Any] | None:
    """`None` if `command` should be dispatched -- either the device has
    positively reported supporting it, or its command set isn't known yet
    (see module docstring's "Deliberately NOT a dispatch-blocking check for
    the unknown case"). Otherwise a `{"ok": False, "error": ..., "reason_code":
    ...}` envelope, returned by the hub BEFORE the command ever reaches the
    device -- the fast-fail this handshake exists to enable, instead of
    letting `background.js`'s own if-chain fall through to a bare
    `"unsupported command: X"` with no hint the extension is stale.
    """
    if device_commands is None or command in device_commands:
        return None
    return {
        "ok": False,
        "error": (
            f"'{command}' is not in this device's reported command set -- the EXTENSION is "
            "behind this hub. Update the extension (browser_update_extension tool, or see "
            'INSTALL.md\'s "Updating" section), then retry.'
        ),
        "reason_code": "device_command_unsupported",
    }


__all__ = ["SkewReport", "capability_error", "compute_skew", "describe_skew"]
